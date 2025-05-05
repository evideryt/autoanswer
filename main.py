2025-05-05 10:29:39,315 - __main__ - INFO - Received business message from Анти300(6056868639) in chat 6056868639
2025-05-05 10:29:54,320 - __main__ - INFO - Debounce timer expired for chat 6056868639. Preparing to send history.
2025-05-05 10:29:54,320 - asyncio - ERROR - Task exception was never retrieved
future: <Task finished name='Task-107' coro=<send_history_to_owner() done, defined at /opt/render/project/src/main.py:62> exception=TypeError("'datetime.datetime' object cannot be interpreted as an integer")>
Traceback (most recent call last):
  File "/opt/render/project/src/main.py", line 76, in send_history_to_owner
    time_str = format_timestamp(timestamp)
               ^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/render/project/src/main.py", line 57, in format_timestamp
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
TypeError: 'datetime.datetime' object cannot be interpreted as an integer