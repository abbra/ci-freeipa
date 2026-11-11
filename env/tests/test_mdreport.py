from freeipa_env import mdreport


def test_pipe_in_a_cell_does_not_split_the_row():
    assert mdreport._cell('a|b\nc') == 'a\\|b c'


def test_fence_is_longer_than_backticks_in_the_text():
    out = mdreport._fence('x ```` y')
    assert out.startswith('`````\n') and out.endswith('\n`````')
