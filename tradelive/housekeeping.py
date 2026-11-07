"""Keeps disk use bounded over months of daily use."""

from __future__ import annotations

import glob
import logging
import os
import shutil
import sys
import tempfile
import time

from tradelive.settings import app_dir

log = logging.getLogger("tradelive")

KEEP_DAYS = 10  # recorded OI / warm-up caches older than this are deleted


def clean_recorded_data(keep_days: int = KEEP_DAYS) -> int:
  """Deletes data/live files (per-day OI recordings and warm-up caches)
  older than `keep_days`. Returns how many files were removed."""
  cutoff = time.time() - keep_days * 86400
  removed = 0
  for path in glob.glob(os.path.join(app_dir(), "data", "live", "*.json")):
    try:
      if os.path.getmtime(path) < cutoff:
        os.remove(path)
        removed += 1
    except OSError:
      pass
  return removed


def clean_stale_pyinstaller_dirs(max_age_hours: float = 1.0) -> int:
  """The one-file Windows exe unpacks itself to %TEMP%\\_MEIxxxx and removes
  that folder on a normal exit, but NOT when it is killed (Task Manager,
  shutdown). Remove such leftovers; folders in use are locked and skipped."""
  if not getattr(sys, "frozen", False):
    return 0
  mine = os.path.normcase(os.path.abspath(getattr(sys, "_MEIPASS", "")))
  cutoff = time.time() - max_age_hours * 3600
  removed = 0
  for path in glob.glob(os.path.join(tempfile.gettempdir(), "_MEI*")):
    if os.path.normcase(os.path.abspath(path)) == mine:
      continue
    try:
      if os.path.getmtime(path) < cutoff and os.path.isfile(
          os.path.join(path, "base_library.zip")):  # looks like PyInstaller
        shutil.rmtree(path)
        removed += 1
    except OSError:
      pass
  return removed


def run_all() -> None:
  n1 = clean_recorded_data()
  n2 = clean_stale_pyinstaller_dirs()
  if n1 or n2:
    log.info("housekeeping: removed %d old data file(s), %d temp folder(s)",
             n1, n2)
