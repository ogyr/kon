"""Fleet-compatible HTTP server for Kon.

Exposes the OpenCode-compatible REST + SSE API that the opencode-fleet
orchestrator expects from worker instances.  Start with::

    kon-serve --port 4097

Or programmatically::

    from kon.server.app import create_app
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=4097)
"""
