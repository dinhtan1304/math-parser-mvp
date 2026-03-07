"""
Shared pytest fixtures for parser pipeline tests.
"""
import asyncio
import pytest


@pytest.fixture(scope="session")
def event_loop():
    """Single event loop for entire session (avoids 'attached to different loop' errors)."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
