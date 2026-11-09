"""Caches that keep the assistant's answers fast.

- `ttl.TTLCache`: a small in-process LRU with expiry, for results that
  are cheap to lose (looked-up passages, tool results). There is one
  backend process, so nothing needs sharing between workers.
- `answers.AnswerCache`: answers to knowledge-base questions, saved after
  they are given once and served again in well under a second, plus the
  questions and answers admins pin (FAQ).
"""
