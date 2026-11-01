"""媒体设置区块只能出现在「媒体」页，不能挂在所有页面底下。

这个 bug 犯过两次：区块被放在某个页面容器的 `</div>` **之后**，于是不属于任何一个
页面，切换标签时它一直挂在那里。第一次插错了容器（落进「数据看板」），因为收尾
锚点 `</div></div></div>` 在好几个页面都长一样。

所以这里不比对整段 HTML，只查两件事：
1. 整份页面里这个区块**只有一份**；
2. 它的位置落在 `pageMedia` 容器的起止区间内。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, extra))


def main():
    print("media settings section lives inside the media page only")
    with open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8") as handle:
        html = handle.read()

    # 用区块里独有的控件 id 定位，title 文案在 i18n 字典里也出现，不能用它。
    marker = 'id="setMediaDailyCredits"'
    check("section appears exactly once", html.count(marker) == 1, html.count(marker))
    if html.count(marker) != 1:
        print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
        return 1

    pages = [
        "pageGateway", "pageAccounts", "pageTasks", "pageMedia",
        "pageAnalytics", "pageAgents", "pageSettings", "pageLogs",
    ]
    starts = {}
    for name in pages:
        index = html.find('<div id="%s"' % name)
        if index != -1:
            starts[name] = index
    check("media page container found", "pageMedia" in starts)

    target = html.find(marker)
    ordered = sorted((index, name) for name, index in starts.items())
    owner = None
    for index, name in ordered:
        if index < target:
            owner = name
        else:
            break
    check("section is inside the media page", owner == "pageMedia", owner)

    # 反向确认：其它页面容器的区间里不能有它
    for index, name in ordered:
        if name == "pageMedia" or index > target:
            continue
        later = [i for i, _ in ordered if i > index]
        if not later:
            continue
        end = min(later)
        check("%s does not contain it" % name,
              not (index < target < end), "%d < %d < %d" % (index, target, end))

    # div 配平：漏一个 </div> 会让后续页面整块塌进当前页
    for name in ("pageMedia",):
        index = starts.get(name, -1)
        if index == -1:
            continue
        later = [i for i, _ in ordered if i > index]
        body = html[index:min(later)] if later else html[index:]
        opens = len(re.findall(r"<div\b", body))
        closes = len(re.findall(r"</div>", body))
        check("%s tags balanced" % name, opens == closes, "%d/%d" % (opens, closes))

    print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
