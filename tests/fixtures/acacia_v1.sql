-- Original Acacia state schema; this is a source contract, not run data.

CREATE TABLE simulator_instances(instance_id TEXT PRIMARY KEY,platform_account TEXT NOT NULL,schema_version INTEGER NOT NULL);
CREATE TABLE scenario_runs(simulation_run_id TEXT PRIMARY KEY,scenario_id TEXT NOT NULL,state_version INTEGER NOT NULL,retired INTEGER NOT NULL,state TEXT NOT NULL);
CREATE TABLE provider_objects(simulation_run_id TEXT NOT NULL,account_id TEXT NOT NULL,kind TEXT NOT NULL,object_id TEXT NOT NULL,version INTEGER NOT NULL,document TEXT NOT NULL,PRIMARY KEY(simulation_run_id,kind,object_id),FOREIGN KEY(simulation_run_id) REFERENCES scenario_runs);
CREATE TABLE idempotency(simulation_run_id TEXT NOT NULL,account_id TEXT NOT NULL,method TEXT NOT NULL,path TEXT NOT NULL,key TEXT NOT NULL,request_sha256 TEXT NOT NULL,resource_id TEXT,status INTEGER NOT NULL,response_projection TEXT NOT NULL,PRIMARY KEY(simulation_run_id,account_id,method,path,key),FOREIGN KEY(simulation_run_id) REFERENCES scenario_runs);
CREATE TABLE webhook_outbox(delivery_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,simulation_run_id TEXT NOT NULL,payload BLOB NOT NULL,payload_sha256 TEXT NOT NULL,delivery_state TEXT NOT NULL,attempts INTEGER NOT NULL,ordinal INTEGER NOT NULL UNIQUE,FOREIGN KEY(simulation_run_id) REFERENCES scenario_runs);
