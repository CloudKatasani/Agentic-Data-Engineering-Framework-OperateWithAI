"""The scenario library: realistic nights for this estate. Each scenario injects named
faults into the simulated world at a set time; several can run in the same night."""

from __future__ import annotations

from .world import World

SCENARIOS = [
    {"id": "S01", "title": "GoldenGate replicat abends after an ERP release",
     "story": "Last night's ERP release (CHG0031877) added a column to GL_JE_LINES. At 00:50 the first row using it reaches "
              "REP_SNF, which abends with OGG-01296. Finance data stops flowing to Snowflake.",
     "tools": ["Oracle", "GoldenGate", "Snowflake", "dbt", "Denodo", "Power BI", "Cognos"],
     "faults": [("00:50", "ogg.ddl_mismatch", {})]},
    {"id": "S02", "title": "Informatica policy load hits a Db2 deadlock",
     "story": "wf_POLICY_DAILY_LOAD fails at 01:20 with SQL0911N: a Db2 online REORG housekeeping job holds locks on the policy table.",
     "tools": ["IBM Db2", "Informatica", "dbt", "Power BI", "Cognos"],
     "faults": [("00:35", "db2.lock_contention", {})]},
    {"id": "S03", "title": "Oracle service account password expires (one cause, two tools)",
     "story": "At 01:10 the SVC_DATA password expires on the ERP database. GoldenGate extract abends (OGG-00664) and "
              "Informatica wf_GL_CONTROL_TOTALS fails (CMN_1022). Both report ORA-28001.",
     "tools": ["Oracle", "GoldenGate", "Informatica", "dbt", "Power BI", "Cognos"],
     "faults": [("01:10", "oracle.account_expired", {})]},
    {"id": "S04", "title": "Snowflake resource monitor suspends the transform warehouse",
     "story": "An ad-hoc query burned the month's remaining credits. At 02:10 RM_TRANSFORM reaches its quota and suspends "
              "WH_TRANSFORM, 20 minutes before the dbt nightly build needs it.",
     "tools": ["Snowflake", "dbt", "Power BI", "Cognos"],
     "faults": [("02:10", "snowflake.rm_quota", {})]},
    {"id": "S05", "title": "Claims vendor sends the file twice",
     "story": "The claims file arrives at 01:05 at twice its normal size: yesterday's rows are repeated. If it loads, the dbt "
              "unique test on fct_claims fails and Claims Aging is held.",
     "tools": ["MFT", "Informatica", "dbt", "Cognos"],
     "faults": [("00:35", "mft.duplicate_file", {})]},
    {"id": "S06", "title": "Power BI gateway VM goes down in Azure",
     "story": "At 04:40 Dynatrace reports host gw-az-01 unavailable after a failed guest OS update. The Finance Daily Close "
              "refresh at 05:00 cannot reach its data source.",
     "tools": ["Azure", "Dynatrace", "Power BI"],
     "faults": [("04:40", "azure.vm_down", {})]},
    {"id": "S07", "title": "Cognos report service is saturated",
     "story": "From 05:20 the Cognos batch report service has hung processes. The Regulatory Liquidity burst fails with DPR-ERR-2109.",
     "tools": ["Cognos"],
     "faults": [("05:20", "cognos.dispatcher_overload", {})]},
    {"id": "S08", "title": "Denodo cache load fails with an error nobody has seen before",
     "story": "The 04:00 Denodo cache job fails with a connection pool error that is not in the known error database. "
              "The engineer fixes it by hand and the team learns a new known error.",
     "tools": ["Denodo", "Snowflake", "Power BI"],
     "faults": [("03:50", "denodo.pool_exhausted", {})]},
    {"id": "S09", "title": "Backup agent starves the Informatica server",
     "story": "At 00:55 Dynatrace reports CPU saturation on infa-prd-01: a backup agent is running outside its window. "
              "Nothing has failed, but every load runs at half speed and the morning deadlines are at risk.",
     "tools": ["AWS", "Dynatrace", "Informatica", "dbt"],
     "faults": [("00:55", "host.cpu_saturation", {})]},
    {"id": "S10", "title": "Claims vendor file is late",
     "story": "The claims file misses its 01:30 cutoff and arrives at 03:40. Nothing is broken on our side; the work is "
              "chasing the vendor, re-planning the batch and telling the claims team.",
     "tools": ["MFT", "Informatica", "dbt", "Cognos"],
     "faults": [("00:35", "mft.file_late", {"arrives": "03:40"})]},
]
PRESETS = {
    "quiet": {"title": "Quiet night", "scenarios": []},
    "typical": {"title": "Typical bad night", "scenarios": ["S03", "S05", "S06"]},
    "everything": {"title": "Everything at once", "scenarios": ["S01", "S02", "S04", "S05", "S06", "S07", "S08"]},
}
BY_ID = {s["id"]: s for s in SCENARIOS}


def inject(world: World, scenario_ids: list[str]) -> None:
    for sid in scenario_ids:
        for at, condition, detail in BY_ID[sid]["faults"]:
            world.schedule(at, condition, **detail)
