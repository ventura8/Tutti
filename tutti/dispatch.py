import time
import os
import signal
import subprocess
import logging
from concurrent.futures import ThreadPoolExecutor
import random

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def dispatch_build():
    # Simulate a build command that might fail due to transient issues
    if os.getenv('FAIL_BUILD', 'false').lower() == 'true':
        raise Exception("Build failed due to transient issues")
    logger.info("Build succeeded!")

def _with_retry(fn, attempts=4, base=0.5, factor=2, max_delay=30, jitter=True):
    """
    Run a function with exponential backoff retry logic.
    
    Args:
        fn: The function to be retried.
        attempts: Maximum number of retries allowed.
        base: Base delay in seconds for the first retry.
        factor: Exponent for the exponential backback calculation.
        max_delay: Maximum delay in seconds between retries.
        jitter: Whether to add random jitter to the delay.
    
    Returns:
        The result of the function if successful, or None if all retries failed.
    """
    retry_count = 0
    while retry_count < attempts:
        try:
            return fn()
        except Exception as e:
            logger.error(f"Attempt {retry_count + 1} failed: {e}")
            retry_count += 1
            
            if retry_count == attempts:
                logger.error("All retries failed, giving up.")
                return None
            
            # Calculate delay with jitter
            delay = min(base * (factor ** (retry_count - 1)), max_delay)
            if jitter:
                delay += random.uniform(0, delay * 0.2)
            
            logger.info(f"Retrying in {delay:.2f} seconds...")
            time.sleep(delay)
    return None

def run_with_retry(fn, max_retries=3, base_delay=1.0, exponent=2, max_delay=60.0, jitter=True):
    """
    Run a function with exponential backoff retry logic.
    
    Args:
        fn: The function to be retried.
        max_retries: Maximum number of retries allowed.
        base_delay: Base delay in seconds for the first retry.
        exponent: Exponent for the exponential backoff calculation.
        max_delay: Maximum delay in seconds between retries.
        jitter: Whether to add random jitter to the delay.
    
    Returns:
        The result of the function if successful, or None if all retries failed.
    """
    retry_count = 0
    while retry_count < max_retries:
        try:
            return fn()
        except Exception as e:
            logger.error(f"Attempt {retry_count + 1} failed: {e}")
            retry_count += 1
            
            if retry_count == max_retries:
                logger.error("All retries failed, giving up.")
                return None
            
            # Calculate delay with jitter
            delay = min(base_delay * (exponent ** (retry_count - 1)), max_delay)
            if jitter:
                delay += random.uniform(0, delay * 0.2)
            
            logger.info(f"Retrying in {delay:.2f} seconds...")
            time.sleep(delay)
    return None

if __name__ == "__main__":
    run_with_retry(dispatch_build)
