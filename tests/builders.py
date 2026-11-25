"""Construtores de dados no formato exato da REST API do Veeam (v12.3 / 1.2-rev1)."""
from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc).replace(microsecond=0)
_ids = itertools.count(1)


def uid() -> str:
    return str(uuid.UUID(int=next(_ids)))


def ts(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).astimezone(timezone(timedelta(hours=-3))).isoformat()


def job(name, vms=(), hosts=(), disabled=False, daily="21:00", after=None, job_id=None):
    includes = [{"type": "VirtualMachine", "name": v, "hostName": "10.0.0.1", "objectId": str(i), "size": "10 GB"}
                for i, v in enumerate(vms)]
    includes += [{"type": "Host", "name": h, "hostName": h, "objectId": "ha-host"} for h in hosts]
    sched = {"runAutomatically": True, "daily": {"isEnabled": after is None, "dailyKind": "Everyday", "localTime": daily},
             "afterThisJob": {"isEnabled": after is not None, "jobName": after},
             "retry": {"isEnabled": True, "retryCount": 3, "awaitMinutes": 10}}
    return {"id": job_id or uid(), "name": name, "type": "Backup", "isDisabled": disabled, "description": "",
            "schedule": sched, "virtualMachines": {"includes": includes, "excludes": {"vms": []}}}


def state(j, status="inactive", last_result="Success", next_run_h=None):
    return {"id": j["id"], "name": j["name"], "type": "Backup", "status": status, "lastResult": last_result,
            "lastRun": ts(1), "nextRun": ts(-next_run_h) if next_run_h else None, "repositoryName": "Repo1",
            "workload": "vm", "objectsCount": len(j["virtualMachines"]["includes"]), "description": ""}


def session(j, start_h, end_h, result="Success", msg="", state_="Stopped", progress=100):
    return {"id": uid(), "jobId": j["id"], "name": j["name"], "sessionType": "BackupJob", "state": state_,
            "creationTime": ts(start_h), "endTime": ts(end_h) if end_h is not None else None,
            "progressPercent": progress, "result": {"result": result, "message": msg, "isCanceled": False}}


def task(sess, vm, start_h, end_h, result="Success", msg="", state_="Stopped"):
    return {"id": uid(), "sessionId": sess["id"], "type": "Backup", "sessionType": "BackupJob", "name": vm,
            "state": state_, "creationTime": ts(start_h), "endTime": ts(end_h) if end_h is not None else None,
            "result": {"result": result, "message": msg or result, "isCanceled": False}}


def backup(j=None, name=None, platform="VMware", job_id=None):
    return {"id": uid(), "jobId": job_id or (j["id"] if j else "00000000-0000-0000-0000-000000000000"),
            "name": name or (j["name"] if j else "?"), "platformName": platform, "creationTime": ts(1000)}


def rp(b, vm, hours_ago, type_="Increment", malware="Clean"):
    return {"id": uid(), "backupId": b["id"], "name": vm, "creationTime": ts(hours_ago), "type": type_,
            "malwareStatus": malware, "platformName": b["platformName"], "sessionId": uid()}
