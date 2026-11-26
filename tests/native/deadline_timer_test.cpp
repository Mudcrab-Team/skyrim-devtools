// Exercises native/deadline_timer.h without the game: one thread, ordered firing, clean stop.
#include "deadline_timer.h"

#include <atomic>
#include <cstdio>
#include <cstdlib>

#ifdef __linux__
#include <dirent.h>
static int ThreadCount()
{
    int n = 0;
    if (auto* d = opendir("/proc/self/task")) {
        while (auto* e = readdir(d)) n += e->d_name[0] != '.';
        closedir(d);
    }
    return n;
}
#else
static int ThreadCount() { return -1; }
#endif

#define CHECK(c) do { if (!(c)) { std::fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #c); std::exit(1); } } while (0)

int main()
{
    using namespace std::chrono;
    const int before = ThreadCount();
    std::atomic_int fired = 0;
    {
        devtools::DeadlineTimer timer;
        for (int i = 0; i < 500; ++i) {
            CHECK(timer.Add(steady_clock::now() + milliseconds(1 + i % 20), [&] { ++fired; }));
        }
        if (before >= 0) CHECK(ThreadCount() == before + 1);
        std::this_thread::sleep_for(milliseconds(200));
        CHECK(fired == 500);
        timer.Add(steady_clock::now() + hours(1), [&] { fired += 1000; });
        timer.Stop();
        CHECK(!timer.Add(steady_clock::now(), [&] { fired += 1000; }));
        timer.Stop();
    }
    CHECK(fired == 500);
    if (before >= 0) CHECK(ThreadCount() == before);
    std::puts("ok");
}
