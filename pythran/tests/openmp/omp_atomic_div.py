def omp_atomic_div():
    sum = 1024.
    LOOPCOUNT = 10000
    "omp parallel for"
    for i in xrange(1,LOOPCOUNT):
        "omp atomic"
        sum /= i
    return sum
