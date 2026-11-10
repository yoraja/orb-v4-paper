"""YOPC-primary / box-backup fencing for ORB v4 PAPER.

Per-host claim files are not a lock across machines. Trading requires a
confirmed fencing token from git compare-and-swap on the existing paper repo.
If that token cannot be confirmed, callers fail closed and submit nothing.
"""
