import re

from pair.diff import FileDiff, hunk_ref
from pair.util import is_doc

RISKY = {
    "process": re.compile(r"\b(kill|pkill|killall|Kill(Session|Window|Pane|Server)|os\.Exit|Process\.kill|"
                          r"signal\.SIG(KILL|TERM))\b"),
    "delete": re.compile(r"(rm -rf|os\.RemoveAll?\b|shutil\.rmtree|DROP (TABLE|COLUMN|INDEX)|DELETE FROM|"
                         r"TRUNCATE\b|\.destroy_all|\.delete_all|\bunlink\(|FileUtils\.rm)", re.I),
    "exec": re.compile(r"(\bexec\.Command|\bsubprocess\.|\bos\.system|\bOpen3\.|`[^`\n]+`|\bsystem\(|"
                       r"\bchild_process|\bspawn\(|\bexecSync\()"),
    "migration": re.compile(r"\b(ALTER TABLE|CREATE (UNIQUE )?INDEX|add_column|remove_column|create_table|"
                            r"drop_table|rename_column|change_column)\b", re.I),
    "network": re.compile(r"(\bhttp\.(Get|Post|NewRequest)|\brequests\.(get|post|put|delete|request)|\burllib|"
                          r"\bfetch\(|Net::HTTP|Faraday|HTTParty|\baxios\b|\bhttpx\.)"),
}
MIGRATION_PATH = re.compile(r"(^|/)(migrations?|migrate)/", re.I)


def risky_hunks(files: list[FileDiff]) -> list[dict]:
    out = []
    for f in files:
        if is_doc(f.path):
            continue
        for h in f.hunks:
            added = "\n".join(code for _, code in h.added)
            tags = [k for k, rx in RISKY.items() if rx.search(added)]
            if MIGRATION_PATH.search(f.path) and "migration" not in tags:
                tags.append("migration")
            if tags:
                out.append({"ref": hunk_ref(f.path, h), "path": f.path, "line": h.target_start, "tags": tags,
                            "added": added[:3000]})
    return out
