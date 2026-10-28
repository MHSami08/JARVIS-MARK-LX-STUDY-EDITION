"""Self-test: recursive folder approval, forbidden paths, fuzzy search, file_reader.
Run from the project root:  python tools/test_folder_access.py
Uses a temporary manifest - your real data/approved_files.json is never touched."""
import sys, tempfile, pathlib, shutil, atexit
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import core.security_policy as sp

# Not the system temp dir: on Windows that lives under AppData, which the
# gatekeeper (correctly) refuses to approve.
(ROOT / "runtime").mkdir(exist_ok=True)
tmp = pathlib.Path(tempfile.mkdtemp(prefix="_selftest_", dir=str(ROOT / "runtime")))
atexit.register(shutil.rmtree, str(tmp), True)
sp.APPROVED_MANIFEST = tmp / "manifest.json"
root = tmp / "StudyNotes"
(root / "phy" / "ch1" / "deep").mkdir(parents=True)
(root / ".git").mkdir()
(root / ".git" / "config").write_text("x")
(root / ".env").write_text("S=1")
(root / "phy" / "ch1" / "deep" / "zqxtest_red_car.txt").write_text("car notes")
(root / "bio_notes.md").write_text("# bio")
(tmp / "StudyNotes-private").mkdir()
(tmp / "StudyNotes-private" / "s.txt").write_text("s")

def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)

sp.approve_folder_permanently(str(root))
check(sp.is_path_approved(str(root / "phy/ch1/deep/zqxtest_red_car.txt")), "nested file covered by folder grant")
check(not sp.is_path_approved(str(tmp / "StudyNotes-private/s.txt")), "sibling with same prefix NOT covered")
for bad in (root / ".env", root / ".git" / "config"):
    try:
        sp.request_verified_file_access(str(bad)); check(False, f"{bad.name} blocked")
    except PermissionError:
        check(True, f"{bad.name} blocked inside approved folder")
names = [e["relative"] for e in sp.list_directory_tree(str(root))["entries"]]
check("phy/ch1/deep/zqxtest_red_car.txt" in names and not any(".git" in n or ".env" in n for n in names),
      "recursive listing, secrets hidden")
import actions.smart_file_search as sfs
# Unique word: your real Pictures/Downloads may hold other files named "car".
r = sfs.resolve("zqxtest")
check(r["status"] == "ok" and r["path"].endswith("zqxtest_red_car.txt"),
      "fuzzy partial name 'zqxtest' -> zqxtest_red_car.txt (no extension needed)")
r = sfs.resolve("RED CAR zqxtest")
check(any(m["path"].endswith("zqxtest_red_car.txt") for m in r["matches"]),
      "case-insensitive multi-word query finds it")
import actions.file_reader as fr
check(fr.execute("read_file", file_path="bio notes")["status"] == "ok", "file_reader resolves plain words")
check(fr.execute("list_folder", folder_path="studynotes")["count"] >= 4, "folder resolved by name")
print("ALL OK")
