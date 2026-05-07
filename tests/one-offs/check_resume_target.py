"""Check what path cmd_resume would cd to for AMD_INTIGRITI session."""
import sys, os
sys.path.insert(0, r'C:\code\claude-projects\Claude-Session-Backup')
from unittest.mock import patch
from claude_session_backup.commands import cmd_resume
from claude_session_backup.index import get_session, open_db, init_schema

REAL_DB = r'C:\Users\Extreme\.claude\session-backup.db'

class Args:
    session_id = 'f2d0d074'
    quiet = False
    claude_dir = None
    db = REAL_DB

conn = open_db(REAL_DB)
init_schema(conn)
s = get_session(conn, 'f2d0d074')
if s is None:
    print("ERROR: session not found in real DB")
    sys.exit(1)
sf = s.get('start_folder')
jp = s.get('jsonl_path')
print(f"start_folder: {sf}")
print(f"jsonl_path: {jp}")
conn.close()

execvp_called = []
def mock_execvp(prog, args):
    execvp_called.append((prog, args))

chdir_called = []
def mock_chdir(path):
    chdir_called.append(path)

with patch('os.execvp', side_effect=mock_execvp), \
     patch('os.chdir', side_effect=mock_chdir):
    rc = cmd_resume(Args())
    cd_target = chdir_called[0] if chdir_called else None
    print(f"chdir called with: {cd_target}")
    print(f"execvp args: {execvp_called}")
    print(f"exit code: {rc}")
    # Compare to start_folder
    if cd_target and cd_target != sf:
        print("PASS: slug-decoded path differs from start_folder (11.4 scenario)")
        print(f"  start_folder={sf}")
        print(f"  cd_target={cd_target}")
    elif cd_target == sf:
        print("OK: cd_target matches start_folder (no slug disambiguation needed)")
