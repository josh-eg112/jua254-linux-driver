#!/usr/bin/env python3
"""Offline regression for the default-off compositor phase experiment."""
from jua254_service import command, read_config
from jua254_stream import (Metric, presentation_phase_wait_seconds,
                           presentation_phase_deadline, wait_for_presentation_phase)

REFRESH_HZ=60.0
PERIOD=1.0/REFRESH_HZ

def test_requested_wait_invariant():
    # offsets about the currently-targeted neighborhood, plus wraparound
    offsets=(-2.0,0.0,2.0,1000.0,-1000.0,0.001,-0.001)
    # push `now` far past a single period to exercise modulo wraparound
    views=[(10.0,10.0),(10.0,10.001),(10.0,10.020),(10.0,99.5),(100.0,100.0),
           (100.0,100.017),(1.9,1000.0),(0.0,5.5)]
    for offset in offsets:
        for ptime,now in views:
            wait=presentation_phase_wait_seconds(ptime,now,offset,REFRESH_HZ)
            assert wait is not None
            assert 0.0<=wait<PERIOD, (offset,ptime,now,wait)

def test_deadline_alignment():
    period=1/60
    assert presentation_phase_deadline(10.0,10.001,0)==10.0+period
    deadline=presentation_phase_deadline(10.0,10.020,2.0)
    assert abs(deadline-(10.002+2*period))<1e-12
    assert presentation_phase_deadline(None,10.0,0) is None
    assert presentation_phase_deadline(10.0,10.0,None) is None
    assert 0 <= presentation_phase_deadline(10.0,10.001,1000)-10.001 <= period
    # +/-2.0 ms requested waits are always < one refresh period
    for offset in (-2.0,0.0,2.0):
        wait=presentation_phase_wait_seconds(10.0,10.001,offset)
        assert 0.0<=wait<period

def test_offset_phase_displacement():
    # A +2.0 ms offset delays the phase by 2 ms; -2.0 ms advances it, both
    # keeping a strictly positive requested wait inside one refresh period.
    base=presentation_phase_wait_seconds(10.0,10.001,0.0)
    plus=presentation_phase_wait_seconds(10.0,10.001,2.0)
    minus=presentation_phase_wait_seconds(10.0,10.001,-2.0)
    assert plus>=0.0 and minus>=0.0 and base<PERIOD and plus<PERIOD and minus<PERIOD
    # When the phase train is born far in the future the requested wait wraps.
    wrapped=presentation_phase_wait_seconds(80.0,10.001,0.0)
    assert 0.0<=wrapped<PERIOD

def test_refresh_period_rollover_boundary():
    # Exactly on a phase slot the requested wait must be 0 (>=0, <period);
    # just before / just after a slot stays within [0, period).
    assert presentation_phase_wait_seconds(10.0,10.0,0.0)==0.0
    just_after=presentation_phase_wait_seconds(10.0,10.0+1e-9,0.0)
    assert 0.0<=just_after<PERIOD
    just_before=presentation_phase_wait_seconds(10.0+PERIOD,10.0+PERIOD-1e-9,0.0)
    assert 0.0<=just_before<PERIOD

def test_wait_is_bounded_and_optional():
    clock=iter((10.020,10.020,10.035333333333,10.035333333333))
    sleeps=[]; metric=Metric(); lateness=Metric()
    deadline=wait_for_presentation_phase(10.0,2.0,metric,lateness_metric=lateness,
                                         now=lambda:next(clock),sleep=sleeps.append)
    assert abs(deadline-10.035333333333)<1e-9
    assert 0 <= sleeps[0] <= 1/60
    assert metric.report()["count"]==1
    assert lateness.report()["count"]==1
    assert wait_for_presentation_phase(None,0,metric) is None

def test_requested_and_lateness_are_separated():
    # The scheduler proposes an in-bound requested wait; the simulated OS wakes
    # 3 ms after that deadline, so lateness (actual-requested) is +3 ms while
    # the requested metric itself stays inside one refresh period.
    started=10.005
    requested=presentation_phase_wait_seconds(10.0,started,0.0)
    assert 0.0<requested<PERIOD
    wake=started+requested+3.0e-3
    clock=iter((started,wake))
    requested_metric=Metric(); lateness_metric=Metric()
    wait_for_presentation_phase(10.0,0.0,requested_metric,lateness_metric=lateness_metric,
                                now=lambda:next(clock),sleep=lambda _:None)
    requested_ms=requested_metric.report()["mean"]
    lateness_ms=lateness_metric.report()["mean"]
    assert 0.0<=requested_ms<PERIOD*1000.0
    assert abs(requested_ms-requested*1000.0)<1e-9
    assert abs(lateness_ms-3.0)<1e-9

def test_service_default_off_and_explicit_zero(tmp_path):
    defaults=read_config(tmp_path/"missing.toml")
    assert defaults["presentation_phase_offset_ms"] is None
    assert "--presentation-phase-offset-ms" not in command(defaults,[])
    configured=tmp_path/"configured.toml"
    configured.write_text("presentation_phase_offset_ms = 0.0\n")
    args=command(read_config(configured),[])
    index=args.index("--presentation-phase-offset-ms")
    assert args[index+1]=="0.0"

if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    test_requested_wait_invariant(); test_deadline_alignment()
    test_offset_phase_displacement(); test_refresh_period_rollover_boundary()
    test_wait_is_bounded_and_optional(); test_requested_and_lateness_are_separated()
    with tempfile.TemporaryDirectory() as directory:
        test_service_default_off_and_explicit_zero(Path(directory))
    print("presentation phase tests: PASS")