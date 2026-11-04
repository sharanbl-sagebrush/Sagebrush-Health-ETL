"""Start a rebuild (decision D18) as a one-off execution of the acquire job.

    python deploy/rebuild.py finance.monthly_payments            # plan: counts only
    python deploy/rebuild.py finance.monthly_payments --apply    # write
    python deploy/rebuild.py finance.monthly_payments --apply --wait

``az containerapp job start`` with a command override replaces the whole
container template of that execution, so the job's image and environment
(the SQL settings and the secret references) have to be passed again or
the execution starts with none of them (2026-11-04, the first attempt).
This reads them from the job and passes them back, changing only the
command: ``sh scripts/job.sh rebuild-plan <keys>`` or ``rebuild <keys>``.
The scheduled executions are untouched.

``--wait`` polls the execution until it ends and prints its log. The
exit code is 1 when the execution did not succeed. Needs ``az`` signed
in with rights on the resource group; nothing here touches the database
directly.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

RG = "sbh-data-bank-rg"
JOB = "sbh-acquire"


def az(*args: str) -> str:
    result = subprocess.run(
        ["az", *args, "--only-show-errors"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("keys", nargs="+", help="spec keys to rebuild")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--job", default=JOB)
    parser.add_argument("--resource-group", default=RG)
    args = parser.parse_args()

    container = json.loads(
        az("containerapp", "job", "show", "-n", args.job, "-g", args.resource_group,
           "--query", "properties.template.containers[0]", "-o", "json")
    )  # fmt: skip
    env = []
    for e in container.get("env") or []:
        if e.get("secretRef"):
            env.append(f"{e['name']}=secretref:{e['secretRef']}")
        else:
            env.append(f"{e['name']}={e.get('value', '')}")
    mode = "rebuild" if args.apply else "rebuild-plan"
    command = ["sh", "scripts/job.sh", mode, *args.keys]
    print(f"starting {mode} of {', '.join(args.keys)} on {container['image']}")
    name = az(
        "containerapp", "job", "start", "-n", args.job, "-g", args.resource_group,
        "--image", container["image"], "--command", *command, "--env-vars", *env,
        "--query", "name", "-o", "tsv",
    ).strip()  # fmt: skip
    print(f"execution {name}")
    if not args.wait:
        return 0
    status = ""
    while True:
        status = az(
            "containerapp", "job", "execution", "list", "-n", args.job, "-g", args.resource_group,
            "--query", f"[?name=='{name}'].properties.status | [0]", "-o", "tsv",
        ).strip()  # fmt: skip
        print(f"  {time.strftime('%H:%M:%S')} {status}")
        if status not in ("Running", "Processing", "Pending", "Unknown", ""):
            break
        time.sleep(60)
    log = az(
        "containerapp", "job", "logs", "show", "-n", args.job, "-g", args.resource_group,
        "--container", args.job, "--execution", name, "--format", "text",
    )  # fmt: skip
    for line in log.splitlines():
        if " stdout F " in line or " stderr F " in line:
            print(line.split(" F ", 1)[1])
    return 0 if status == "Succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
