"""Ephemeral capabilities and validation for synthetic browser flows."""
import ipaddress
import html
import hmac
import re
import secrets
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .wire import SimulatorError, need
from . import wire


def local_url(value):
    try:
        need(isinstance(value,str) and 0 < len(value) <= 1024 and all(32 < ord(c) < 127 for c in value) and "\\" not in value)
        p = urlsplit(value)
        address = ipaddress.ip_address(p.hostname or "")
        need(p.scheme in {"http","https"} and address.is_loopback and p.hostname == str(address)
             and p.port is not None and not p.username and not p.password and not p.fragment)
        return value
    except ValueError:
        need(False)


def append_query(url, values):
    local_url(url)
    p = urlsplit(url)
    existing = parse_qsl(p.query,keep_blank_values=True)
    need(not any(k in {"code","state","error"} for k, _ in existing))
    return urlunsplit((p.scheme,p.netloc,p.path,urlencode(existing + list(values.items())),""))


def form_origins(*urls):
    origins = set()
    for url in urls:
        try:
            p = urlsplit(local_url(url))
            origins.add(p.scheme + '://' + p.netloc)
        except SimulatorError:
            pass
    return ' '.join(sorted(origins))


class HostedRegistry:
    def __init__(self, clock):
        self.clock = clock
        self.entries = {}
        self.lock = threading.RLock()

    def __repr__(self):
        return "HostedRegistry([REDACTED])"

    def issue(self, purpose, run, account=None, resource=None, data=None, stable=False, lifetime=300):
        with self.lock:
            self._purge()
            if stable:
                for token, entry in self.entries.items():
                    if (entry['purpose'],entry['run'],entry['account'],entry['resource']) == (purpose,run,account,resource):
                        return token
            if purpose != 'oauth':
                self.admit()
            need(len(self.entries) < 1024,429,"simulator_limit")
            token = secrets.token_urlsafe(32)
            self.entries[token] = dict(purpose=purpose,run=run,account=account,resource=resource,
                                       data=data or {},csrf=secrets.token_urlsafe(32),expires=self.clock()+lifetime)
            return token

    def _purge(self):
        for token in list(self.entries):
            if self.entries[token]['expires'] <= self.clock():
                del self.entries[token]

    def admit(self):
        """Ensure one slot before a durable API create can commit."""
        with self.lock:
            self._purge()
            if len(self.entries) >= 1024:
                pending = next((token for token,entry in self.entries.items() if entry['purpose'] == 'oauth'),None)
                if pending is not None:
                    del self.entries[pending]
            need(len(self.entries) < 1024,429,"simulator_limit")

    def get(self, token, purpose):
        with self.lock:
            self._purge()
            entry = self.entries.get(token)
            need(entry is not None and entry['purpose'] == purpose,403,"simulator_capability")
            return dict(entry)

    def remove(self, token):
        with self.lock:
            self.entries.pop(token,None)

    def expire_run(self, run):
        with self.lock:
            for token in list(self.entries):
                if self.entries[token]['run'] == run:
                    del self.entries[token]

    def close(self):
        with self.lock:
            self.entries.clear()


def page(title, content):
    return ("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">"
            "<title>"+html.escape(title)+"</title><style>body{font:16px system-ui;background:#f5f6fa;color:#1a2340;margin:0}"
            "main{max-width:38rem;margin:8vh auto;padding:2rem;background:white;border-radius:12px}"
            "button{background:#635bff;color:white;border:0;border-radius:6px;padding:.75rem 1rem;margin:.5rem .5rem .5rem 0;cursor:pointer}"
            "select{padding:.6rem;max-width:100%}small{color:#566}li{margin:.7rem 0}</style><main><h1>"+
            html.escape(title)+"</h1><p>Test mode: synthetic data only. No real payment is collected.</p>"+content+"</main></html>").encode()


def hidden(name, value):
    return '<input type="hidden" name="'+html.escape(name,quote=True)+'" value="'+html.escape(str(value),quote=True)+'">'


def browser(handler, outer, origin):
    """Handle a browser request after host, framing and size validation."""
    parsed = urlsplit(handler.path)
    form = wire.pairs(handler.rfile.read(int(handler.headers.get("Content-Length","0")))) if handler.command == "POST" else {}
    if handler.command == "POST":
        need(handler.headers.get("Content-Type") == "application/x-www-form-urlencoded")
        need(handler.headers.get("Origin") == origin,403,"simulator_origin")
    with outer._control_lock:
        if parsed.path == "/oauth/authorize" and handler.command == "GET":
            params = wire.pairs(parsed.query.encode("ascii"))
            wire.closed(params,{"client_id","response_type","scope","state","redirect_uri","run"})
            need(params["client_id"] == "ca_LocalSim"+outer.store.instance_id.replace("-","") and params["response_type"] == "code" and params["scope"] == "read_write")
            need(re.fullmatch(r"[A-Za-z0-9._~-]{1,256}",params["state"]) is not None)
            append_query(params["redirect_uri"],{"state":params["state"]})
            outer.store.run(params["run"],status=403)
            token = outer.hosted.issue("oauth",params["run"],data={"state":params["state"],"redirect_uri":params["redirect_uri"]})
            entry = outer.hosted.get(token,"oauth")
            accounts = outer.store.objects(params["run"],"account")
            options = ''.join('<option value="'+html.escape(x["id"],quote=True)+'">'+html.escape(x["id"])+'</option>' for x in accounts)
            content = '<p>Select a synthetic connected account.</p><form method="post" action="/hosted/oauth/'+token+'">'+hidden("_csrf",entry["csrf"])
            content += '<label>Account <select name="account">'+options+'</select></label><p><button name="action" value="approve">Authorize</button><button name="action" value="deny">Deny</button></p></form>'
            return 200,page("Synthetic Connect authorization",content),None,form_origins(params["redirect_uri"])
        match = re.fullmatch(r"/hosted/(checkout|portal|oauth)/([A-Za-z0-9_-]{43})",parsed.path)
        need(match is not None and not parsed.query,404,"simulator_resource")
        purpose, token = match.groups()
        entry = outer.hosted.get(token,purpose)
        run, account, resource = entry["run"],entry["account"],entry["resource"]
        outer.store.run(run,status=403)
        if handler.command == "POST":
            need(re.fullmatch(r"[A-Za-z0-9_-]{43}",form.get("_csrf","")) is not None,403,"simulator_csrf")
            need(hmac.compare_digest(form.get("_csrf",""),entry["csrf"]),403,"simulator_csrf")
            if purpose == "oauth":
                wire.closed(form,{"_csrf","action"},{"account"})
                need(form["action"] in {"approve","deny"})
                values = {"state":entry["data"]["state"]}
                if form["action"] == "approve":
                    need("account" in form)
                    outer.store.object(run,"account",form["account"],form["account"])
                    outer.hosted.remove(token)
                    code = outer.hosted.issue("code",run,form["account"])
                    values["code"] = code
                else:
                    values["error"] = "access_denied"
                destination = append_query(entry["data"]["redirect_uri"],values)
                outer.hosted.remove(token)
                return 303,b"",destination,''
            if purpose == "checkout":
                wire.closed(form,{"_csrf","version","action"})
                session = outer.store.object(run,"checkout_session",resource,account)
                need(form["action"] in {"complete","cancel"})
                destination = local_url(session.get("local_success_url") if form["action"] == "complete" else session.get("local_cancel_url"))
                if form["action"] == "complete":
                    need(re.fullmatch(r"[1-9][0-9]{0,9}",form["version"]) is not None)
                    outer.store.checkout_action(run,account,resource,"complete",int(form["version"]))
                    outer.hosted.remove(token)
                    destination = destination.replace("{CHECKOUT_SESSION_ID}",resource)
                return 303,b"",destination,''
            wire.closed(form,{"_csrf","version","subscription","action"})
            need(re.fullmatch(r"[1-9][0-9]{0,9}",form["version"]) is not None)
            portal, _, _ = outer.store.portal_view(run,account,resource)
            destination = local_url(portal["return_url"])
            outer.store.portal_cancel(run,account,resource,form["subscription"],form["action"],int(form["version"]))
            return 303,b"",destination,''
        need(handler.command == "GET" and purpose != "oauth")
        if purpose == "checkout":
            session = outer.store.object(run,"checkout_session",resource,account)
            need(session["status"] == "open" and outer.clock() < session["expires_at"],409,"simulator_state")
            content = '<p>Complete this simulated subscription checkout.</p><form method="post">'+hidden("_csrf",entry["csrf"])+hidden("version",session["version"])
            content += '<button name="action" value="complete">Complete checkout</button><button name="action" value="cancel">Cancel</button></form>'
            return 200,page("Synthetic checkout",content),None,form_origins(session.get("local_success_url"),session.get("local_cancel_url"))
        portal, documents, enabled = outer.store.portal_view(run,account,resource)
        content = '<h2>Subscriptions</h2>'
        for doc in documents:
            if doc["object"] != "subscription":
                continue
            content += '<p>'+html.escape(doc["id"])+' — '+html.escape(doc["status"])+(' (cancellation scheduled)' if doc.get("cancel_at_period_end") else '')+'</p>'
            if enabled and doc["status"] != "canceled":
                action = "resume" if doc.get("cancel_at_period_end") else "cancel"
                content += '<form method="post">'+hidden("_csrf",entry["csrf"])+hidden("version",doc["version"])+hidden("subscription",doc["id"])
                content += '<button name="action" value="'+action+'">'+('Resume subscription' if action == 'resume' else 'Cancel at period end')+'</button></form>'
        content += '<h2>Invoices</h2><ul>' + ''.join('<li>'+html.escape(d["id"])+' — '+html.escape(d["status"])+'</li>' for d in documents if d["object"] == "invoice")+'</ul>'
        try:
            local_url(portal["return_url"])
            content += '<p><a href="'+html.escape(portal["return_url"],quote=True)+'">Return to application</a></p>'
        except SimulatorError:
            pass
        return 200,page("Synthetic customer portal",content),None,form_origins(portal["return_url"])
