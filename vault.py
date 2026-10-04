#!/usr/bin/env python3
"""
AI Vault v2 — приватна комірка пам'яті для ШІ-агента.

Що вміє:
  1. Розрізати ключ (256 біт) на 5 частин — Shamir, треба 3. Ключ цілим НЕ лежить.
  2. Комірка = СТРУКТУРОВАНА пам'ять: кожен запис має номер і час, накопичується.
  3. Хеш-ланцюг (ledger.jsonl) росте з кожним записом: кожен новий запис підписує
     попередній. Зміниш минуле — усі наступні посилання рвуться (міні-блокчейн).
  4. write АВТОМАТИЧНО дублює зашифровану комірку в mirrors/ — «кожен захід агента =
     нове дублювання». Шифротекст можна множити скільки завгодно (стерти не вийде,
     прочитати без 3 частин ключа — теж).

Команди:
  init | write "текст" | read | mirror [шлях...] | chain | status
"""

import os
import sys
import json
import time
import hashlib
from cryptography.fernet import Fernet

BASE = os.path.dirname(os.path.abspath(__file__))
SHARES_DIR = os.path.join(BASE, "shares")
VAULT_FILE = os.path.join(BASE, "vault.enc")
LEDGER_FILE = os.path.join(BASE, "ledger.jsonl")
MIRRORS_DIR = os.path.join(BASE, "mirrors")

N_PARTS = 5
K_NEEDED = 3
RECOVER_INDEXES = (1, 3, 5)


# ---------- Shamir's Secret Sharing над GF(2^8) ----------
def gf_mul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def gf_pow(a, e):
    r = 1
    while e:
        if e & 1:
            r = gf_mul(r, a)
        a = gf_mul(a, a)
        e >>= 1
    return r


def gf_div(a, b):
    return gf_mul(a, gf_pow(b, 254))


def split(secret, n, k):
    shares = [[] for _ in range(n)]
    for byte in secret:
        coeffs = [byte] + [os.urandom(1)[0] for _ in range(k - 1)]
        for x in range(1, n + 1):
            y = 0
            for c in reversed(coeffs):
                y = gf_mul(y, x) ^ c
            shares[x - 1].append(y)
    return [bytes(s) for s in shares]


def recover(pairs):
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    length = len(ys[0])
    out = []
    for i in range(length):
        acc = 0
        for j in range(len(xs)):
            num = 1
            den = 1
            for m in range(len(xs)):
                if m != j:
                    num = gf_mul(num, xs[m])
                    den = gf_mul(den, xs[j] ^ xs[m])
            acc ^= gf_mul(gf_div(num, den), ys[j][i])
        out.append(acc)
    return bytes(out)


def assemble_key():
    pairs = []
    for idx in RECOVER_INDEXES:
        with open(os.path.join(SHARES_DIR, f"share_{idx}.bin"), "rb") as fh:
            pairs.append((idx, fh.read()))
    return recover(pairs)


# ---------- комірка ----------
def load_entries(key):
    with open(VAULT_FILE, "rb") as fh:
        return json.loads(Fernet(key).decrypt(fh.read()).decode())


def save_entries(key, entries):
    data = json.dumps(entries, ensure_ascii=False).encode()
    with open(VAULT_FILE, "wb") as fh:
        fh.write(Fernet(key).encrypt(data))


# ---------- хеш-ланцюг ----------
def _canon(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()


def sha256_hex(b):
    return hashlib.sha256(b).hexdigest()


def load_ledger():
    rows = []
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def append_ledger(event, cell_hash, n_entries):
    rows = load_ledger()
    prev = sha256_hex(_canon(rows[-1])) if rows else ""
    rec = {
        "index": len(rows),
        "ts": int(time.time()),
        "event": event,
        "cell_sha256": cell_hash,
        "entries": n_entries,
        "prev": prev,
    }
    with open(LEDGER_FILE, "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def verify_chain():
    rows = load_ledger()
    broken = []
    prev = ""
    for r in rows:
        if r.get("prev") != prev:
            broken.append(r.get("index"))
        prev = sha256_hex(_canon(r))
    return broken


def cell_hash():
    with open(VAULT_FILE, "rb") as fh:
        return sha256_hex(fh.read())


def mirror(extra_paths=None):
    made = []
    targets = [MIRRORS_DIR] + (extra_paths or [])
    for t in targets:
        os.makedirs(t, exist_ok=True)
        if t == MIRRORS_DIR:
            dest = os.path.join(t, f"vault_{int(time.time())}.enc")
        else:
            dest = os.path.join(t, "vault.enc")
        with open(VAULT_FILE, "rb") as src, open(dest, "wb") as dst:
            dst.write(src.read())
        made.append(dest)
    return made


# ---------- команди ----------
def cmd_init():
    os.makedirs(SHARES_DIR, exist_ok=True)
    key = Fernet.generate_key()
    parts = split(key, N_PARTS, K_NEEDED)
    for i, p in enumerate(parts, start=1):
        with open(os.path.join(SHARES_DIR, f"share_{i}.bin"), "wb") as fh:
            fh.write(p)
    save_entries(key, [])
    append_ledger("genesis", cell_hash(), 0)
    mirror()
    print(f"OK: ключ створено (256 біт), розрізано на {N_PARTS} частин (треба {K_NEEDED})")
    print("OK: комірку створено, ланцюг почато (генезис), перше дублювання зроблено")


def cmd_write(text):
    key = assemble_key()
    entries = load_entries(key)
    entries.append({"id": len(entries), "ts": int(time.time()), "text": text})
    save_entries(key, entries)
    append_ledger("write", cell_hash(), len(entries))
    made = mirror()
    print(f"OK: записано. Всього записів: {len(entries)}")
    print(f"OK: дублювання: {len(made)} копій комірки оновлено")


def cmd_read():
    key = assemble_key()
    entries = load_entries(key)
    print(f"OK: комірку відкрито. Записів: {len(entries)}")
    for e in entries:
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(e["ts"]))
        print(f"  [{e['id']}] {t} UTC — {e['text']}")


def cmd_mirror():
    extra = sys.argv[2:]
    made = mirror(extra)
    print(f"OK: комірку продубльовано в {len(made)} місць:")
    for m in made:
        print(f"  {m}")


def cmd_chain():
    rows = load_ledger()
    broken = verify_chain()
    print(f"Ланцюг: {len(rows)} блоків. Цілісність: "
          f"{'ПОРУШЕНО в ' + str(broken) if broken else 'ціла'}")
    for r in rows[-5:]:
        p = r["prev"][:12] if r["prev"] else "(генезис)"
        print(f"  #{r['index']} {r['event']:<8} entries={r['entries']} "
              f"cell={r['cell_sha256'][:12]}… prev={p}")


def cmd_status():
    entries = "?"
    if os.path.exists(VAULT_FILE):
        try:
            entries = len(load_entries(assemble_key()))
        except Exception:
            entries = "?"
    rows = load_ledger()
    broken = verify_chain()
    n_shares = len(os.listdir(SHARES_DIR)) if os.path.isdir(SHARES_DIR) else 0
    n_mirrors = len(os.listdir(MIRRORS_DIR)) if os.path.isdir(MIRRORS_DIR) else 0
    print("СТАН системи:")
    print(f"  комірка vault.enc: {'є' if os.path.exists(VAULT_FILE) else 'немає'}")
    print(f"  записів у комірці: {entries}")
    print(f"  частин ключа: {n_shares} з {N_PARTS} (треба {K_NEEDED})")
    print(f"  ланцюг: {len(rows)} блоків, цілісність: {'ціла' if not broken else 'ПОРУШЕНО'}")
    print(f"  дублікатів комірки: {n_mirrors}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Команди: init | write "текст" | read | mirror [шлях...] | chain | status')
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "init":
        cmd_init()
    elif cmd == "write":
        if len(sys.argv) < 3:
            print('Треба текст: python3 vault.py write "текст"')
            sys.exit(1)
        cmd_write(" ".join(sys.argv[2:]))
    elif cmd == "read":
        cmd_read()
    elif cmd == "mirror":
        cmd_mirror()
    elif cmd == "chain":
        cmd_chain()
    elif cmd == "status":
        cmd_status()
    else:
        print("Невідома команда. Можна: init / write / read / mirror / chain / status")
