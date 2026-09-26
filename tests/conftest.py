
import pytest

@pytest.fixture(autouse=True)
def reset_click_stream_cache():
    yield
    try:
        import click.utils
        for cell in getattr(click.utils._default_text_stdout, '__closure__', ()):
            if hasattr(cell.cell_contents, 'clear'):
                cell.cell_contents.clear()
        for cell in getattr(click.utils._default_text_stderr, '__closure__', ()):
            if hasattr(cell.cell_contents, 'clear'):
                cell.cell_contents.clear()
    except Exception:
        pass
