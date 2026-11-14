/* SPDX-License-Identifier: AGPL-3.0-or-later
 * Force-included into the Linux build of the minimal OpenCV (packaging/linux/build_opencv_linux.sh).
 *
 * The Linux edition supports glibc 2.34 (RHEL/Rocky/Alma 9) and later. Building on Ubuntu 22.04
 * (glibc 2.35) binds hypotf to the new symbol version GLIBC_2.35, which glibc 2.34 does not have, so
 * cv2 would fail to load there. The original version (GLIBC_2.2.5) is still exported by every newer
 * glibc and computes the same function; binding to it keeps the measured minimum at 2.34.
 *
 * Likewise for libstdc++: Ubuntu 22.04's runtime (from GCC 12) gives std::condition_variable::wait a
 * second version, GLIBCXX_3.4.30, which GCC 11's runtime (RHEL 9) lacks. The original
 * GLIBCXX_3.4.11 version is still exported and is the same function, so the bundle keeps needing
 * at most GLIBCXX 3.4.29, as before. */
#if defined(__x86_64__) && defined(__linux__)
__asm__(".symver hypotf,hypotf@GLIBC_2.2.5");
#ifdef __cplusplus
__asm__(".symver _ZNSt18condition_variable4waitERSt11unique_lockISt5mutexE,"
        "_ZNSt18condition_variable4waitERSt11unique_lockISt5mutexE@GLIBCXX_3.4.11");
#endif
#endif
