Automatic error-recovery helpers now use the same native spawn admission as
explicit children. Recovery cannot bypass the parent's reviewed fleet, allowed
targets, depth or total-attempt limits, and retains caller identity and cancellation accounting.
