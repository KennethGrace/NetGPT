#!/bin/bash

# Run the server with debug mode
uvicorn netgpt:application --host 0.0.0.0 --port 49488 --reload --log-level debug
```