"""Use native context-local home/secrets; never mirror credentials into process env."""
from contextlib import contextmanager


@contextmanager
def profile_scope(home):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import build_profile_secret_scope, set_secret_scope, reset_secret_scope
    ht = set_hermes_home_override(str(home))
    st = None
    try:
        st = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
        yield
    finally:
        if st is not None:
            reset_secret_scope(st)
        reset_hermes_home_override(ht)
