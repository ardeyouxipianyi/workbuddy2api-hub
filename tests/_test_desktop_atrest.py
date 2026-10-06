"""桌面端 at-rest 加密凭据：信封解密、keyblob 自检、内存密钥回收。

    python tests/_test_desktop_atrest.py

keyblob 相关用例直接给模块喂内容（不落盘），所以整个套件不写任何文件；
内存回收那一组会临时拉起一个 python 进程当靶子，把密钥放进它的内存里再找回来
——不需要装/登录 WorkBuddy 客户端，Windows 之外自动跳过。
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import wb_atrest as A  # noqa: E402

#: 靶子进程：密钥放在模块全局（等价于客户端把它留在常驻内存里），再分配一大块
#: 噪声内存，逼扫描真的动手找而不是碰运气。
HOLDER = r"""
import hashlib, os, sys, time
mode, value, noise_mb, life = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
if mode == "secret":
    SECRET = value
    KEY = hashlib.sha256(value.encode("utf-8")).digest()
else:
    KEY = bytes.fromhex(value)
NOISE = bytearray(os.urandom(noise_mb << 20))
sys.stdout.write(str(os.getpid()) + "\n")
sys.stdout.flush()
time.sleep(life)
"""


def b64_32():
    """客户端 atRestSecretKey 的形状：32 字节的规范 base64（44 字符）。"""
    return base64.b64encode(os.urandom(32)).decode("ascii")


def make_keyblob(secret):
    """按客户端 keyblob 的结构造一份（内容是内存里的 dict，不落盘）。"""
    key = hashlib.sha256(secret.encode("utf-8")).digest()
    master = os.urandom(32)
    return key, master, {
        "version": 1,
        "keyId": A.derive_key_id(master),
        "slots": [{
            "type": "static-v1",
            "protectorKeyId": A.derive_key_id(key),
            "wrapped": A.seal(key, master, "file")["envelope"],
        }],
    }


class EnvelopeTests(unittest.TestCase):
    """信封格式与 AES-256-GCM（对齐客户端 packages/at-rest-crypto）。"""

    def setUp(self):
        self.key = os.urandom(32)

    def test_round_trip(self):
        for text in ("hello", "", "中文昵称", "x" * 3000):
            sealed = A.seal(self.key, text, "field")
            self.assertEqual(A.decrypt_field(self.key, sealed), text)

    def test_envelope_shape(self):
        sealed = A.seal(self.key, "v", "field")
        self.assertEqual(sealed["$wbEncrypted"], 1)
        env = A.parse_envelope(sealed)
        self.assertEqual(env["keyId"], A.derive_key_id(self.key))
        self.assertEqual(len(env["nonce"]), 12)
        self.assertEqual(len(env["authTag"]), 16)

    def test_framing_is_authenticated(self):
        """同一把钥匙，file 帧封的信封不能当 field 帧解开。"""
        sealed = A.seal(self.key, "v", "file")
        with self.assertRaises(A.AtRestError):
            A.open_envelope(self.key, sealed, "field")

    def test_wrong_key_rejected(self):
        sealed = A.seal(self.key, "v", "field")
        with self.assertRaises(A.AtRestError):
            A.open_envelope(os.urandom(32), sealed, "field")

    def test_tampered_ciphertext_rejected(self):
        sealed = A.seal(self.key, "v", "field")
        raw = json.loads(base64.b64decode(sealed["envelope"]).decode("utf-8"))
        ct = bytearray(base64.b64decode(raw["ciphertext"]))
        ct[0] ^= 0x40
        raw["ciphertext"] = base64.b64encode(bytes(ct)).decode("ascii")
        sealed["envelope"] = base64.b64encode(json.dumps(raw).encode("utf-8")).decode("ascii")
        with self.assertRaises(A.AtRestError):
            A.open_envelope(self.key, sealed, "field")

    def test_plaintext_passthrough(self):
        """加密之前的老客户端写的是明文，必须原样返回（向后兼容）。"""
        self.assertEqual(A.decrypt_field(self.key, "plain.token.here"), "plain.token.here")
        self.assertEqual(A.decrypt_field(self.key, ""), "")
        self.assertIsNone(A.decrypt_field(self.key, {"unexpected": 1}))

    def test_is_envelope(self):
        self.assertTrue(A.is_envelope(A.seal(self.key, "v", "field")))
        self.assertTrue(A.is_envelope('{"$wbEncrypted":1,"envelope":"AA=="}'))
        self.assertFalse(A.is_envelope("a.b.c"))
        self.assertFalse(A.is_envelope({"nickname": "x"}))

    def test_aes256_block_vector(self):
        """FIPS-197 C.3 的 AES-256 单块向量，确认分组实现没写反。"""
        key = bytes(range(32))
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        want = bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")
        self.assertEqual(A._AES256(key).encrypt_block(plain), want)

    def test_string_and_bytes_seal_agree(self):
        self.assertEqual(A.seal(self.key, "abc", "field", nonce=b"\x07" * 12),
                         A.seal(self.key, b"abc", "field", nonce=b"\x07" * 12))


class KeyblobTests(unittest.TestCase):
    """keyblob 自检：密钥对不上 protectorKeyId 时必须报错，而不是解出垃圾。"""

    def setUp(self):
        self.secret = b64_32()
        self.key, self.master, self.blob = make_keyblob(self.secret)

    def _patched(self):
        return mock.patch.object(A, "load_keyblob", lambda: self.blob)

    def test_read_key_id(self):
        with self._patched():
            self.assertEqual(A.read_key_id(), A.derive_key_id(self.key))

    def test_unwrap(self):
        with self._patched():
            self.assertEqual(A.unwrap_keyblob(self.key), self.master)

    def test_wrong_key_rejected(self):
        with self._patched():
            with self.assertRaises(A.AtRestError):
                A.unwrap_keyblob(os.urandom(32))

    def test_keyblob_without_slot(self):
        self.blob["slots"] = []
        with self._patched():
            with self.assertRaises(A.AtRestError):
                A.read_key_id()

    def test_document_round_trip(self):
        """整份 .info：加密字段解出来必须是原文，明文域原样保留。"""
        access = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1LTEifQ.sig"
        doc = {
            "account": {"uid": "u-1", "nickname": A.seal(self.key, "小明", "field"),
                        "phoneNumber": A.seal(self.key, "+8613800000000", "field")},
            "auth": {"accessToken": A.seal(self.key, access, "field"),
                     "refreshToken": A.seal(self.key, access + "2", "field"),
                     "domain": "copilot.tencent.com", "expiresAt": 1760000000000},
        }
        self.assertEqual(A.decrypt_field(self.key, doc["account"]["nickname"]), "小明")
        self.assertEqual(A.decrypt_field(self.key, doc["auth"]["accessToken"]), access)
        self.assertEqual(A.decrypt_field(self.key, doc["auth"]["domain"]), "copilot.tencent.com")

    def test_status_reports_key(self):
        with self._patched():
            info = A.status()
            self.assertEqual(info["keyId"], A.derive_key_id(self.key))
            self.assertFalse(info["keyCached"])              # 还没回收过
            A._KEY_CACHE["key"] = self.key                   # 模拟回收完成
            try:
                self.assertTrue(A.status()["keyCached"])
            finally:
                A.forget_key()

    def test_keyblob_path_is_confined(self):
        """配置目录可以来自环境变量，但文件名只能是 keyblob、且必须就在该目录下。"""
        with mock.patch.dict(os.environ, {"WORKBUDDY_CONFIG_DIR": os.path.abspath(os.sep)}):
            path = A.keyblob_path()
        self.assertEqual(os.path.basename(path), "keyblob")
        self.assertEqual(os.path.dirname(path), os.path.realpath(os.path.abspath(os.sep)))

    def test_missing_keyblob_raises(self):
        with mock.patch.object(A, "config_dir", lambda: os.path.join(ROOT, "_no_such_dir")):
            info = A.status()
            self.assertFalse(info["keyblobFound"])
            self.assertTrue(info["error"])
            with self.assertRaises(A.AtRestError):
                A.read_key_id()


class PathTests(unittest.TestCase):
    """路径一律钉死在给定目录内。"""

    def test_traversal_rejected(self):
        base = os.path.abspath(os.sep if os.name != "nt" else "C:" + os.sep)
        for bad in ("../secrets.txt", "..\\secrets.txt", "a/../../b", "sub/../x"):
            with self.assertRaises(A.AtRestError):
                A._safe_path(base, bad)

    def test_basename_is_taken(self):
        base = os.path.abspath(os.sep if os.name != "nt" else "C:" + os.sep)
        self.assertEqual(os.path.dirname(A._safe_path(base, "keyblob")), os.path.realpath(base))


@unittest.skipUnless(os.name == "nt", "内存扫描只支持 Windows")
class HuntTests(unittest.TestCase):
    """真刀真枪：拉起一个靶子进程，把密钥从它的内存里找回来。"""

    NOISE_MB = 48

    def setUp(self):
        self.secret = b64_32()
        self.key, _, self.blob = make_keyblob(self.secret)
        self.children = []
        A.forget_key()
        self._patch = mock.patch.object(A, "load_keyblob", lambda: self.blob)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        for proc in self.children:
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:
                pass
        A.forget_key()

    def _holder(self, mode, value, noise_mb=None):
        proc = subprocess.Popen(
            [sys.executable, "-c", HOLDER, mode, value,
             str(self.NOISE_MB if noise_mb is None else noise_mb), "120"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(proc)
        pid = int((proc.stdout.readline() or "0").strip() or 0)
        self.assertTrue(pid, "靶子进程没起来")
        return pid

    def _hunt(self, pid, **kw):
        """把进程选举换成靶子 pid，其余走真实扫描链路。"""
        with mock.patch.object(A, "_main_pids", lambda *a, **k: [pid]):
            t0 = time.time()
            found = A.hunt_key(key_id=A.derive_key_id(self.key), **kw)
            return found, time.time() - t0

    def test_finds_secret_string_payload(self):
        """第一档：内存里有 44 字符 base64 载荷时，正则档直接捞出来推导。"""
        pid = self._holder("secret", self.secret)
        found, seconds = self._hunt(pid)
        self.assertEqual(found, self.key.hex())
        self.assertLess(seconds, 60, "扫描耗时 %.1fs，明显偏慢" % seconds)

    def test_finds_raw_key_when_payload_is_gone(self):
        """第二档：内存里只剩派生密钥（载荷字符串已不在）时，栅格档仍要找得到。"""
        pid = self._holder("key", self.key.hex())
        found, seconds = self._hunt(pid)
        self.assertEqual(found, self.key.hex())
        self.assertLess(seconds, 120, "扫描耗时 %.1fs，明显偏慢" % seconds)

    def test_recover_key_keeps_it_in_memory(self):
        pid = self._holder("secret", self.secret)
        with mock.patch.object(A, "_main_pids", lambda *a, **k: [pid]):
            key = A.recover_key()
        self.assertEqual(key, self.key)
        self.assertEqual(A.cached_key(), self.key)      # 第二次直接命中缓存
        self.assertNotIn(self.key.hex(), json.dumps(A.status()))   # 状态里不带密钥

    def test_unknown_keyid_raises(self):
        """四档全试完都不命中时必须报错，而不是返回一个假密钥。"""
        pid = self._holder("secret", self.secret, noise_mb=16)
        with mock.patch.object(A, "_main_pids", lambda *a, **k: [pid]):
            with self.assertRaises(A.AtRestError):
                A.hunt_key(key_id="0" * 16, workers=1)

    def test_non_windows_message(self):
        with mock.patch.object(A.os, "name", "posix"):
            with self.assertRaises(A.AtRestError):
                A.hunt_key(key_id="ab" * 8)


class CacheTests(unittest.TestCase):
    def test_forget_without_cache_is_safe(self):
        A.forget_key()
        self.assertIsNone(A.cached_key())
        self.assertTrue(A.forget_key())

    def test_cached_key_revalidates_against_keyblob(self):
        """缓存里的密钥必须每次都用 keyblob 复核：客户端换钥后旧钥立刻作废。"""
        secret = b64_32()
        key, _, blob = make_keyblob(secret)
        A._KEY_CACHE["key"] = key
        try:
            with mock.patch.object(A, "load_keyblob", lambda: blob):
                self.assertEqual(A.cached_key(), key)
            with mock.patch.object(A, "load_keyblob", lambda: make_keyblob(b64_32())[2]):
                self.assertIsNone(A.cached_key())
        finally:
            A.forget_key()


if __name__ == "__main__":
    unittest.main(verbosity=2)
