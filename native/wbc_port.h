#ifndef WBC_PORT_H
#define WBC_PORT_H
/*
 * 跨平台垫片：POSIX 路径逐字不变，Windows 上只补四件事。
 *
 *   wbc_now_seconds()   单调时钟（POSIX: clock_gettime(CLOCK_MONOTONIC)；
 *                       Windows: QueryPerformanceCounter）——只用于 meta 计时。
 *   wbc_process_usage() 进程 CPU 秒与峰值 RSS（POSIX: getrusage(RUSAGE_SELF)；
 *                       Windows: GetProcessTimes + GetProcessMemoryInfo）。
 *   wbc_localtime_r()   localtime 的可重入写法（Windows 是 localtime_s）。
 *   wbc_stdout_binary() stdout 切二进制模式——**只在 Windows 需要**：文本模式会把
 *                       \n 翻成 \r\n，CLI 输出的 JSON 就不再逐字节可比。
 *
 * 目录遍历不需要垫片：mingw-w64 自带 <dirent.h>（opendir/readdir/struct dirent）。
 * 其余用到的 strdup / snprintf / vsnprintf / mktime / strftime 各平台都有。
 *
 * 这些函数是 static：只有 main.c 包含本头文件，没有多定义问题。
 */
#include <stddef.h>
#include <time.h>

#ifdef _WIN32
#include <windows.h>
#include <io.h>
#include <fcntl.h>
#include <psapi.h>
/* localtime_s 返回 errno_t（0 = 成功），调用点按 POSIX 的「指针或 NULL」写，
 * 所以这里包一层把两种语义对齐。 */
static inline struct tm *wbc_localtime_r_win(const time_t *t, struct tm *tm)
{
    return localtime_s(tm, t) == 0 ? tm : NULL;
}
#define wbc_localtime_r(t, tm) wbc_localtime_r_win((t), (tm))
#else
#include <sys/resource.h>
#include <sys/time.h>
#define wbc_localtime_r(t, tm) localtime_r((t), (tm))
#endif

/* 文件 mtime 的纳秒读数：字段名各平台不同（Linux 是 st_mtim、macOS 是
 * st_mtimespec、mingw 只有秒级的 st_mtime）。只当缓存键用，秒级也够。 */
#ifdef _WIN32
#define WBC_STAT_MTIME_NS(st) ((long long)(st).st_mtime * 1000000000LL)
#elif defined(__APPLE__)
#define WBC_STAT_MTIME_NS(st) ((long long)(st).st_mtimespec.tv_sec * 1000000000LL + \
                               (long long)(st).st_mtimespec.tv_nsec)
#else
#define WBC_STAT_MTIME_NS(st) ((long long)(st).st_mtim.tv_sec * 1000000000LL + \
                               (long long)(st).st_mtim.tv_nsec)
#endif

/* 单调时钟，秒。精度只影响 meta 里的计时字段，不参与任何计算。 */
static inline double wbc_now_seconds(void)
{
#ifdef _WIN32
    static LARGE_INTEGER freq;
    LARGE_INTEGER now;
    if (freq.QuadPart == 0) QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&now);
    return (double)now.QuadPart / (double)freq.QuadPart;
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
#endif
}

/* 进程 CPU 秒与峰值 RSS（KiB）。拿得到返回 1，拿不到返回 0（调用方只是不写
 * 那两个 meta 字段）。 */
static inline int wbc_process_usage(double *cpu_seconds, long long *peak_rss_kib)
{
#ifdef _WIN32
    FILETIME created, exited, kernel, user;
    PROCESS_MEMORY_COUNTERS mem;
    ULARGE_INTEGER k, u;
    if (cpu_seconds == NULL || peak_rss_kib == NULL) return 0;
    if (!GetProcessTimes(GetCurrentProcess(), &created, &exited, &kernel, &user))
        return 0;
    k.LowPart = kernel.dwLowDateTime;  k.HighPart = kernel.dwHighDateTime;
    u.LowPart = user.dwLowDateTime;    u.HighPart = user.dwHighDateTime;
    /* FILETIME 是 100ns 单位：kernel+user 是内核态+用户态 CPU 时间。 */
    *cpu_seconds = (double)(k.QuadPart + u.QuadPart) / 1e7;
    if (GetProcessMemoryInfo(GetCurrentProcess(), &mem, sizeof(mem)))
        *peak_rss_kib = (long long)(mem.PeakWorkingSetSize / 1024);
    else
        *peak_rss_kib = 0;
    return 1;
#else
    struct rusage usage;
    if (cpu_seconds == NULL || peak_rss_kib == NULL) return 0;
    if (getrusage(RUSAGE_SELF, &usage) != 0) return 0;
    *cpu_seconds = (double)usage.ru_utime.tv_sec +
                   (double)usage.ru_utime.tv_usec / 1e6 +
                   (double)usage.ru_stime.tv_sec +
                   (double)usage.ru_stime.tv_usec / 1e6;
    *peak_rss_kib = (long long)usage.ru_maxrss;
    return 1;
#endif
}

/* stdout 切二进制。POSIX 上什么都不做（本来就是字节流）。 */
static inline void wbc_stdout_binary(void)
{
#ifdef _WIN32
    _setmode(_fileno(stdout), _O_BINARY);
#endif
}

#endif /* WBC_PORT_H */
