from n5171b import N5171B


with N5171B("192.168.1.100") as source:
    result = source.list_sweep.run_linear_sweep(
        start_mhz=100,
        stop_mhz=1000,
        points=101,
        sweep_time_s=1.01,
    )
    print(result)
