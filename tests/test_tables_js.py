"""The pure helpers of static/tables.js, run under Node (skipped when Node is not installed)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).parents[1] / "inventory/web/static/tables.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def run(expr):
    code = f"const t=require({json.dumps(str(JS))});console.log(JSON.stringify({expr}))"
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_count_text():
    got = run(
        "[t.countText(240,240,false), t.countText(1,1,false), t.countText(0,0,false),"
        " t.countText(12,240,true), t.countText(0,240,true)]"
    )
    assert got == ["240 rows", "1 row", "0 rows", "12 of 240 rows shown", "0 of 240 rows shown"]


def test_matches():
    got = run(
        '[t.matches(["dev","alpha","migrated"],"alpha",-1), t.matches(["dev","alpha"],"dev",1),'
        ' t.matches(["dev","Alpha"],"alpha",1), t.matches(["dev","alpha"],"",1), t.matches(["dev","alpha"],"x",-1),'
        ' t.matches(["dev","alpha"],"dev",5)]'
    )
    assert got == [True, False, True, True, False, False]
