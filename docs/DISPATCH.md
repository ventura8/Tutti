# Dispatch Build Retry Logic

The dispatcher runs once per dashboard probe round, after the autopilot. It also runs the moment a shard ends with exponential backoff.

## Retry Mechanism

The `dispatch build` command implements retry logic with exponential backoff to handle transient failures such as network timeouts or temporary resource unavailability.

### Parameters

- **max_retries**: The maximum number of attempts before giving up (default: 3).
- **base_delay**: The initial delay between retries in seconds (default: 1.0).
- **exponent**: The factor by which the delay increases after each failed attempt (default: 2).
- **max_delay**: The maximum delay allowed between retries to prevent excessive waiting (default: 60.0).
- **jitter**: Whether to add random jitter to the delay to avoid thundering herd problems (default: True).

### Behavior

1. The command attempts to execute the build operation.
2. If a transient error occurs, it waits for a calculated delay and retries.
3. If all retries are exhausted, the command fails with an appropriate error message.

This ensures that temporary issues do not cause immediate failures, improving overall reliability.
