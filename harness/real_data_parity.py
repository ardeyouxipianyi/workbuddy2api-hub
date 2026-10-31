"""用 412 的真实 usage 副本再对拍一次（夹具全绿 ≠ 生产对得上）。"""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, time

spec = importlib.util.spec_from_file_location(
    "par", "/home/ytjungle/workbuddy2api-hub/tests/_test_usage_parity.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

tmp = tempfile.mkdtemp(prefix="wb-real-")
for label in ("py", "rust"):
    os.makedirs(os.path.join(tmp, label, "usage"))
    os.makedirs(os.path.join(tmp, label, "accounts"))
subprocess.run("scp -q -r root@10.10.10.155:/app/usage/* %s/py/usage/" % tmp, shell=True, check=False)
subprocess.run("cp -a %s/py/usage/. %s/rust/usage/" % (tmp, tmp), shell=True, check=False)
subprocess.run("scp -q root@10.10.10.155:/app/accounts/settings.json %s/py/accounts/" % tmp, shell=True, check=False)
subprocess.run("cp -a %s/py/accounts/. %s/rust/accounts/" % (tmp, tmp), shell=True, check=False)

py = mod.Backend("py", [sys.executable, "/home/ytjungle/workbuddy2api-hub/wb_proxy.py",
                        "--host", "127.0.0.1", "--port", "{port}", "--accounts-dir", "{accounts}",
                        "--usage-dir", "{usage}", "--panel-password", "pw"],
                 "/home/ytjungle/workbuddy2api-hub", tmp + "/py/usage", tmp + "/py/accounts")
ru = mod.Backend("rust", [mod.BINARY, "--host", "127.0.0.1", "--port", "{port}",
                          "--accounts-dir", "{accounts}", "--usage-dir", "{usage}"],
                 "/home/ytjungle/workbuddy2api-hub", tmp + "/rust/usage", tmp + "/rust/accounts")
try:
    py.wait_ready("python")
    ru.wait_ready("rust")
    pt, rt = py.token(), ru.token()
    total = 0
    for path in mod.MATRIX:
        ps, pb = py.get(path, pt)
        rs, rb = ru.get(path, rt)
        if ps != rs:
            print("%-42s 状态码 %s != %s" % (path, ps, rs))
            total += 1
            continue
        found = mod.diff(mod.normalise(rb, ru), mod.normalise(pb, py))
        known = mod.KNOWN_DIFFS.get(path, set())
        real = [item for item in found
                if item[0].rsplit("/", 1)[-1].split("[")[0] not in known and "*" not in known]
        total += len(real)
        print("%-42s 差异 %3d 处（未登记 %d）" % (path, len(found), len(real)))
        for step, lv, rv in real[:6]:
            print("    rust%s = %s | python = %s" % (step, lv, rv))
    print("未登记差异合计：", total)
finally:
    py.stop()
    ru.stop()
    shutil.rmtree(tmp, ignore_errors=True)
