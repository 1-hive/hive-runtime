"""The Iter hive channel's state files are written whole or not at all, and one left cut short (by a full disk, before
writes were whole) starts over instead of failing every poll."""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hive_channel", HERE / "adapters" / "iter" / "channels" / "hive.py")
ch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ch)

with tempfile.TemporaryDirectory() as d:
    ch.configure(d)
    ch.RUN.mkdir(parents=True)
    f = ch.RUN / "seen.json"

    ch._save(f, {"t-1": {"status": "assigned"}})
    assert json.loads(f.read_text()) == {"t-1": {"status": "assigned"}}
    assert [p.name for p in ch.RUN.iterdir()] == ["seen.json"], "no temporary file left behind"

    real = Path.write_text

    def full_disk(self, *a, **k):   # the temporary file can't be written: the old state stays as it was
        real(self, "{\"t-")
        raise OSError(28, "No space left on device")
    Path.write_text = full_disk
    try:
        ch._save(f, {"t-2": {}})
        raise AssertionError("the write should have failed")
    except OSError:
        pass
    finally:
        Path.write_text = real
    assert json.loads(f.read_text()) == {"t-1": {"status": "assigned"}}, "a failed write keeps the old state"
    assert [p.name for p in ch.RUN.iterdir()] == ["seen.json"], "the failed temporary file is removed"

    f.write_text("")                                   # cut short by a full disk before this change
    assert ch._load(f, None) is None, "an empty file reads as missing: the next poll is a first poll"
    assert "seen.json unreadable" in (ch.RUN / "log.txt").read_text()
    assert ch._load(ch.RUN / "absent.json", {}) == {}

print("PASSED: state files written whole or not at all; a failed write keeps the old state; one cut short starts over")
sys.exit(0)
