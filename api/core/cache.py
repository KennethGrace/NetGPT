"""
The cache module provides the ability to cache data in memory for a specified
amount of time. This is done via the CacheCore class, which is provides a
simple interface for caching data. This is used to cache the results of
expensive operations, such as the results of a database query, so that the
results can be returned quickly without having to perform the operation again.
This is also done to track the state of the NetGPT Service separately from the
state of the user's chat session. This allows the AI to perform actions "in
the background" without the user having to wait for the action to complete
before the AI can respond to the user's message.
"""