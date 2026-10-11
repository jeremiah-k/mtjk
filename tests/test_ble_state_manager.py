"""Tests for BLEStateManager state machine functionality.

This module is intentionally comprehensive: it exercises transitions, thread
safety, race resistance, and stress/performance behavior in one place to keep
state-machine invariants easy to audit.
"""

import gc
import logging
import os
import threading
import time
import weakref
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from meshtastic.interfaces.ble.state import BLEStateManager, ConnectionState

pytestmark = pytest.mark.unit


@contextmanager
def _suppress_ble_debug_logs() -> Generator[None, None, None]:
    """Temporarily suppress noisy meshtastic.ble debug logs during timing-sensitive tests.

    Performance and memory regression tests should not be dominated by logger
    I/O or emit thousands of transition lines in CI output.
    """
    ble_logger = logging.getLogger("meshtastic.ble")
    original_level = ble_logger.level
    ble_logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        ble_logger.setLevel(original_level)


class TestBLEStateManager:
    """Test cases for BLEStateManager class."""

    def test_initial_state(self) -> None:
        """Test that state manager starts in DISCONNECTED state."""
        manager = BLEStateManager()
        assert cast(Any, manager._current_state) == ConnectionState.DISCONNECTED
        assert not manager._is_connected
        assert not manager._is_closing
        assert manager._can_connect

    def test_state_properties(self) -> None:
        """Test state-based property methods."""
        manager = BLEStateManager()

        # Direct internal assignment keeps this test focused on property semantics
        # instead of transition-validation behavior.
        # Test DISCONNECTED state
        manager._state = ConnectionState.DISCONNECTED
        assert not manager._is_connected
        assert not manager._is_closing
        assert manager._can_connect

        # Test CONNECTED state
        manager._state = ConnectionState.CONNECTED
        assert manager._is_connected
        assert not manager._is_closing
        assert not manager._can_connect

        # Test DISCONNECTING state
        manager._state = ConnectionState.DISCONNECTING
        assert not manager._is_connected
        assert manager._is_closing
        assert not manager._can_connect

        # Test ERROR state
        manager._state = ConnectionState.ERROR
        assert not manager._is_connected
        assert not manager._is_closing
        # ERROR state allows reconnection attempts
        assert manager._can_connect

    def test_valid_transitions(self) -> None:
        """Test all valid state transitions."""
        manager = BLEStateManager()

        # DISCONNECTED → CONNECTING
        assert manager._transition_to(ConnectionState.CONNECTING)
        assert cast(Any, manager._current_state) == ConnectionState.CONNECTING

        # CONNECTING → CONNECTED
        assert manager._transition_to(ConnectionState.CONNECTED)
        assert cast(Any, manager._current_state) == ConnectionState.CONNECTED

        # CONNECTED → DISCONNECTING
        assert manager._transition_to(ConnectionState.DISCONNECTING)
        assert cast(Any, manager._current_state) == ConnectionState.DISCONNECTING

        # DISCONNECTING → DISCONNECTED
        assert manager._transition_to(ConnectionState.DISCONNECTED)
        assert cast(Any, manager._current_state) == ConnectionState.DISCONNECTED

        # DISCONNECTED → ERROR
        assert manager._transition_to(ConnectionState.ERROR)
        assert cast(Any, manager._current_state) == ConnectionState.ERROR

        # ERROR → CONNECTING
        assert manager._transition_to(ConnectionState.CONNECTING)
        assert cast(Any, manager._current_state) == ConnectionState.CONNECTING

    def test_invalid_transitions(self) -> None:
        """Verify that invalid state transitions are rejected while no-op transitions to the same state are allowed.

        Asserts:
        - Transitioning to the current state (no-op) returns True and leaves the state unchanged.
        - Transition from DISCONNECTED to CONNECTED is rejected and leaves state unchanged.
        - Transition from CONNECTED back to CONNECTING is rejected and leaves state unchanged.
        """
        manager = BLEStateManager()

        # No-op transition (same state) is now valid - returns True
        assert manager._transition_to(ConnectionState.DISCONNECTED)
        assert cast(Any, manager._current_state) == ConnectionState.DISCONNECTED

        # Try invalid transition from DISCONNECTED (not in valid transitions)
        assert not manager._transition_to(ConnectionState.CONNECTED)
        assert cast(Any, manager._current_state) == ConnectionState.DISCONNECTED

        # Move to CONNECTED and try invalid transition
        assert manager._transition_to(ConnectionState.CONNECTING)
        assert manager._transition_to(ConnectionState.CONNECTED)
        assert not manager._transition_to(
            ConnectionState.CONNECTING
        )  # Can't go back to CONNECTING from CONNECTED
        assert cast(Any, manager._current_state) == ConnectionState.CONNECTED

    def test_transition_if_in_returns_false_for_unexpected_state(self) -> None:
        """_transition_if_in should reject transitions when state is outside expected set."""
        manager = BLEStateManager()
        manager._state = ConnectionState.CONNECTED

        assert not manager._transition_if_in(
            {ConnectionState.DISCONNECTED}, ConnectionState.CONNECTING
        )
        assert cast(Any, manager._current_state) == ConnectionState.CONNECTED

    @given(
        st.lists(
            st.sampled_from(list(ConnectionState)),
            min_size=1,
            max_size=80,
        )
    )
    @settings(max_examples=100, deadline=None)
    def test_transition_sequence_invariants(
        self, sequence: list[ConnectionState]
    ) -> None:
        """Verify that arbitrary sequences of state transition requests preserve BLEStateManager invariants.

        For each target in `sequence` this test asserts three invariants:
        - The boolean result of transition_to(target) matches whether the manager's state equals the target after the call.
        - If the transition call indicates failure, the manager's state does not change.
        - The manager's state is always a member of ConnectionState.

        Parameters
        ----------
        sequence : list[ConnectionState]
            Ordered collection of target states to request on the manager.
        """
        manager = BLEStateManager()
        for target_state in sequence:
            previous_state = manager._current_state
            transition_succeeded = manager._transition_to(target_state)
            current_state = manager._current_state
            assert transition_succeeded == (current_state == target_state)
            if not transition_succeeded:
                assert current_state == previous_state
            assert current_state in ConnectionState

    def test_thread_safety(self) -> None:
        """Test concurrent state access is thread-safe."""
        manager = BLEStateManager()
        results = []
        errors = []
        append_lock = threading.Lock()

        def worker(worker_id: int) -> None:
            """Perform alternating transitions and record outcomes.

            On each of 100 iterations, tries CONNECTING on even indices and
            DISCONNECTED on odd indices.
            """
            try:
                for i in range(100):
                    # Try to transition states concurrently
                    if i % 2 == 0:
                        success = manager._transition_to(ConnectionState.CONNECTING)
                    else:
                        success = manager._transition_to(ConnectionState.DISCONNECTED)
                    with append_lock:
                        results.append(
                            (worker_id, i, success, manager._current_state.value)
                        )
                    time.sleep(0.001)  # Small delay to increase contention
            except Exception as e:  # noqa: BLE001 - errors collected for assertion
                with append_lock:
                    errors.append((worker_id, str(e)))

        # Start multiple threads
        threads = []
        for i in range(5):
            thread = threading.Thread(target=worker, args=(i,), daemon=True)
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # Verify no errors occurred
        assert len(errors) == 0, f"Errors occurred: {errors}"

        # Verify final state is valid
        assert manager._current_state in ConnectionState

        # Verify all transitions were either successful or failed gracefully
        assert len(results) == 500  # 5 workers * 100 iterations each

    def test_state_transition_logging(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test that state transitions are properly logged."""
        manager = BLEStateManager()

        with caplog.at_level(logging.DEBUG):
            manager._transition_to(ConnectionState.CONNECTING)
            manager._transition_to(ConnectionState.CONNECTED)

        # Check that transitions were logged
        assert "State transition: disconnected → connecting" in caplog.text
        assert "State transition: connecting → connected" in caplog.text

    def test_invalid_transition_logging(self, caplog: pytest.LogCaptureFixture) -> None:
        """Ensure an invalid state transition emits a warning log.

        Specifically, transitioning from DISCONNECTED to CONNECTED should produce the WARNING message: "Invalid state transition: disconnected → connected".
        """
        manager = BLEStateManager()

        with caplog.at_level(logging.WARNING):
            manager._transition_to(
                ConnectionState.CONNECTED
            )  # Invalid from DISCONNECTED

        # Check that invalid transition was logged as warning
        assert "Invalid state transition: disconnected → connected" in caplog.text

    def test_reentrant_lock_behavior(self) -> None:
        """Test that reentrant lock allows nested acquisitions."""
        manager = BLEStateManager()

        def nested_operation() -> ConnectionState:
            """Read and return the manager's current connection state while holding its reentrant state lock.

            Returns
            -------
            ConnectionState
                The current connection state.
            """
            # This should work with reentrant lock
            with manager._state_lock:
                with manager._state_lock:
                    return manager._current_state

        # Should not deadlock
        assert nested_operation() == ConnectionState.DISCONNECTED

    def test_all_state_values(self) -> None:
        """Test that all ConnectionState enum values are accessible."""
        expected_states = {
            "disconnected": ConnectionState.DISCONNECTED,
            "connecting": ConnectionState.CONNECTING,
            "connected": ConnectionState.CONNECTED,
            "disconnecting": ConnectionState.DISCONNECTING,
            "reconnecting": ConnectionState.RECONNECTING,
            "error": ConnectionState.ERROR,
        }

        for value, state in expected_states.items():
            assert state.value == value
            assert isinstance(state, ConnectionState)

    def test_state_consistency_after_error(self) -> None:
        """Verify BLEStateManager maintains consistent state flags after an ERROR transition and can recover to DISCONNECTED.

        After simulating an error during connection, asserts that the manager's state is ERROR, `isConnected` is False, `is_closing` is False, and `can_connect` is True. Then transitions to DISCONNECTED and asserts the manager returns to DISCONNECTED with `can_connect` True.
        """
        manager = BLEStateManager()

        # Simulate error during connection
        manager._transition_to(ConnectionState.CONNECTING)
        manager._transition_to(ConnectionState.ERROR)

        assert cast(Any, manager._current_state) == ConnectionState.ERROR
        assert not manager._is_connected
        assert not manager._is_closing
        assert manager._can_connect  # ERROR state allows reconnection

        # Should be able to recover from error
        manager._transition_to(ConnectionState.DISCONNECTED)
        assert cast(Any, manager._current_state) == ConnectionState.DISCONNECTED
        assert manager._can_connect


class TestBLEInterfaceStateIntegration:
    """Test integration between BLEStateManager and BLEInterface."""

    def test_state_manager_standalone_initialization(self) -> None:
        """Verify BLEStateManager can be instantiated standalone and exhibits expected initial properties and basic transition behavior.

        Checks that a newly created manager starts in DISCONNECTED with can_connect True, isConnected False, is_closing False,
        and no client, and that transitioning to CONNECTING succeeds and updates the state and can_connect flag.
        """

        # Test state manager can be created and used independently
        manager = BLEStateManager()

        # Check initial state
        assert cast(Any, manager._current_state) == ConnectionState.DISCONNECTED
        assert manager._can_connect
        assert not manager._is_connected
        assert not manager._is_closing

        # Test state transitions work
        assert manager._transition_to(ConnectionState.CONNECTING)
        assert cast(Any, manager._current_state) == ConnectionState.CONNECTING
        assert not manager._can_connect

    def test_state_based_property_usage(self) -> None:
        """Test that state-based properties work as expected."""

        manager = BLEStateManager()

        # Test initial state properties
        assert cast(Any, manager._current_state) == ConnectionState.DISCONNECTED
        assert not manager._is_connected
        assert not manager._is_closing
        assert manager._can_connect

        # Test transitioning through states
        manager._transition_to(ConnectionState.CONNECTING)
        assert cast(Any, manager._current_state) == ConnectionState.CONNECTING
        assert not manager._is_connected
        assert not manager._is_closing
        assert not manager._can_connect

        manager._transition_to(ConnectionState.CONNECTED)
        assert cast(Any, manager._current_state) == ConnectionState.CONNECTED
        assert manager._is_connected
        assert not manager._is_closing
        assert not manager._can_connect

        manager._transition_to(ConnectionState.DISCONNECTING)
        assert cast(Any, manager._current_state) == ConnectionState.DISCONNECTING
        assert not manager._is_connected
        assert manager._is_closing
        assert not manager._can_connect

        # Test ERROR state allows reconnection attempts
        manager._transition_to(ConnectionState.ERROR)
        assert cast(Any, manager._current_state) == ConnectionState.ERROR
        assert not manager._is_connected
        assert not manager._is_closing
        assert manager._can_connect  # ERROR state allows reconnection

    def test_client_management_with_states(self) -> None:
        """Verify state transitions through the connection lifecycle.

        Asserts that transitioning from CONNECTING to CONNECTED marks the
        manager as connected, that transitioning to DISCONNECTED resets the
        state, and that a no-op transition to the current DISCONNECTED state
        is considered valid.
        """
        manager = BLEStateManager()

        # Should be able to set client when transitioning to CONNECTED (via CONNECTING)
        assert manager._transition_to(ConnectionState.CONNECTING)
        assert manager._transition_to(ConnectionState.CONNECTED)
        assert manager._is_connected

        # Client should be cleared when transitioning to DISCONNECTED
        assert manager._transition_to(ConnectionState.DISCONNECTED)
        assert cast(Any, manager._current_state) == ConnectionState.DISCONNECTED

        # No-op transition (same state) is now valid - returns True
        assert manager._transition_to(ConnectionState.DISCONNECTED)

    def test_state_transition_validation(self) -> None:
        """Verify that BLEStateManager enforces allowed and disallowed transitions from DISCONNECTED.

        Asserts that transitions permitted from DISCONNECTED (to CONNECTING and ERROR) succeed,
        that direct transitions to CONNECTED, RECONNECTING, or DISCONNECTING are rejected.
        """

        manager = BLEStateManager()

        # Valid transitions from DISCONNECTED
        assert manager._transition_to(ConnectionState.CONNECTING)
        assert manager._transition_to(ConnectionState.DISCONNECTED)  # Reset
        assert manager._transition_to(ConnectionState.ERROR)
        assert manager._transition_to(ConnectionState.DISCONNECTED)  # Reset

        # Invalid transitions from DISCONNECTED
        assert not manager._transition_to(
            ConnectionState.CONNECTED
        )  # Must go through CONNECTING first
        assert not manager._transition_to(
            ConnectionState.RECONNECTING
        )  # Must have been connected first
        assert not manager._transition_to(
            ConnectionState.DISCONNECTING
        )  # Cannot "begin disconnecting" from already-disconnected state


class TestStateLockConsolidation:
    """Test consolidated state locking and concurrent scenarios."""

    def test_unified_lock_thread_safety(self) -> None:
        """Test that unified state lock provides thread safety for concurrent operations."""
        manager = BLEStateManager()
        results = []
        errors = []
        append_lock = threading.Lock()

        def worker(worker_id: int) -> None:
            """Perform a short sequence of state transitions on a shared BLEStateManager and record outcomes.

            Runs 10 iterations requesting CONNECTING, CONNECTED (with a mock client), or DISCONNECTED in round-robin order. Appends a tuple (worker_id, iteration, manager._current_state, success) to the shared `results` list for each iteration; on exception appends (worker_id, error_message) to the shared `errors` list. Intended for invocation from concurrent threads to exercise transition behavior.

            Parameters
            ----------
            worker_id : int
                Identifier included in entries added to `results` and `errors` to distinguish this worker.
            """
            try:
                for i in range(10):
                    # Simulate state transitions
                    if i % 3 == 0:
                        success = manager._transition_to(ConnectionState.CONNECTING)
                    elif i % 3 == 1:
                        success = manager._transition_to(ConnectionState.CONNECTED)
                    else:
                        success = manager._transition_to(ConnectionState.DISCONNECTED)

                    with append_lock:
                        results.append((worker_id, i, manager._current_state, success))
                    time.sleep(0.001)  # Small delay to encourage interleaving
            except Exception as e:  # noqa: BLE001
                with append_lock:
                    errors.append((worker_id, str(e)))

        # Create multiple threads
        threads = []
        for i in range(5):
            thread = threading.Thread(target=worker, args=(i,), daemon=True)
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # Verify no errors occurred
        assert len(errors) == 0, f"Errors occurred: {errors}"

        # Verify all operations completed
        assert len(results) == 50, f"Expected 50 operations, got {len(results)}"

        # Verify final state is valid
        assert manager._current_state in ConnectionState

    def test_reentrant_lock_with_transitions(self) -> None:
        """Verify that the manager's state lock is reentrant and allows nested transitions without deadlock.

        Acquires the manager's internal lock, performs nested CONNECTING → CONNECTED transitions, and asserts the manager is connected and ends in ConnectionState.CONNECTED.
        """
        manager = BLEStateManager()

        # Test nested lock acquisition through state manager methods
        def nested_operation() -> None:
            """Validate that nested state transitions under the manager's reentrant lock complete without deadlock and leave the manager connected.

            Performs a CONNECTING → CONNECTED sequence while the manager's reentrant lock is held (using a mock client) and verifies the manager reports connected and retains the client.
            """
            # This should work without deadlock due to reentrant lock
            assert manager._transition_to(ConnectionState.CONNECTING)
            assert manager._transition_to(ConnectionState.CONNECTED)
            assert manager._is_connected

        # Acquire lock manually then call nested operation
        with manager._state_lock:
            nested_operation()

        # Verify state is consistent
        assert cast(Any, manager._current_state) == ConnectionState.CONNECTED

    def test_lock_contention_resolution(self) -> None:
        """Ensure BLEStateManager's state lock handles concurrent contention without raising exceptions and leaves the manager in DISCONNECTED.

        Spawns multiple threads that each acquire the manager's state lock and perform state transitions; the test asserts no contention-related exceptions occurred and that the final manager._current_state equals ConnectionState.DISCONNECTED.
        """

        manager = BLEStateManager()
        contention_count = 0
        contention_count_lock = threading.Lock()

        def contending_worker() -> None:
            """Perform a short lock-holding workload against the shared manager to exercise lock contention.

            Acquires manager._state_lock, sleeps briefly, transitions the manager to
            ConnectionState.CONNECTING and then to ConnectionState.DISCONNECTED. If an
            unexpected exception occurs, increments ``contention_count``.
            """
            nonlocal contention_count
            try:
                # Try to acquire lock and perform operation
                with manager._state_lock:
                    # Simulate some work
                    time.sleep(0.01)
                    manager._transition_to(ConnectionState.CONNECTING)
                    manager._transition_to(ConnectionState.DISCONNECTED)
            except Exception:  # noqa: BLE001 - contention counts any unexpected failure
                with contention_count_lock:
                    contention_count += 1

        # Create multiple contending threads
        threads = []
        for _i in range(10):
            thread = threading.Thread(target=contending_worker, daemon=True)
            threads.append(thread)
            thread.start()

        # Wait for completion
        for thread in threads:
            thread.join()

        # Verify no contention errors
        assert contention_count == 0

        # Verify final state is consistent
        assert cast(Any, manager._current_state) == ConnectionState.DISCONNECTED


@pytest.mark.unit
@pytest.mark.skipif(
    not os.getenv("RUN_PERF_TESTS"), reason="Performance tests disabled by default"
)
def test_state_transition_performance() -> None:
    """Lightweight performance regression guard for repeated BLE state transitions.

    This is intentionally a fast check (not a full benchmark): it validates
    that transition throughput remains within reasonable bounds and catches
    major regressions early in normal CI runs.
    """
    manager = BLEStateManager()

    # Keep this lightweight and stable under coverage/instrumented CI.
    iterations = 10_000
    with _suppress_ble_debug_logs():
        start_time = time.perf_counter()
        for _i in range(iterations):
            # Cycle through states
            manager._transition_to(ConnectionState.CONNECTING)
            manager._transition_to(ConnectionState.CONNECTED)
            manager._transition_to(ConnectionState.DISCONNECTED)
        end_time = time.perf_counter()
    elapsed = end_time - start_time

    # Calculate average transition time
    avg_time = elapsed / (iterations * 3)
    assert avg_time < 0.00025, f"Average transition time too high: {avg_time:.6f}s"

    logging.info(
        "Performance: %d transitions in %.3fs, avg: %.6fs",
        iterations * 3,
        elapsed,
        avg_time,
    )


@pytest.mark.unit
@pytest.mark.skipif(
    not os.getenv("RUN_PERF_TESTS"), reason="Performance tests disabled by default"
)
def test_lock_contention_performance() -> None:
    """Lightweight lock-contention regression guard for BLEStateManager.

    Spawns 5 worker threads that each perform 200 iterations of CONNECTING → CONNECTED → DISCONNECTED
    transition attempts (3 operations per iteration) with a short delay to simulate work. Asserts the total
    elapsed time stays below a generous CI-safe threshold and that at least 80% of the expected operations
    (5 * 200 * 3) succeeded. Prints a brief performance summary.
    """

    manager = BLEStateManager()
    results = []
    results_lock = threading.Lock()

    def worker(worker_id: int) -> None:
        """Run a fixed sequence of BLE state transitions while measuring this worker's operations and elapsed time.

        Executes 200 iterations where the outer-scope `manager` is asked to transition to CONNECTING, CONNECTED, and DISCONNECTED in sequence; each successful transition increments an operation count. After completion, appends a result dict to the outer-scope `results` list with keys "worker_id", "operations", and "time" (elapsed seconds).

        Parameters
        ----------
        worker_id : int
            Identifier used in the appended result to distinguish this worker.
        """
        start_time = time.perf_counter()
        operations = 0

        for _i in range(200):
            # Simulate realistic state operations
            if manager._transition_to(ConnectionState.CONNECTING):
                operations += 1
            if manager._transition_to(ConnectionState.CONNECTED):
                operations += 1
            if manager._transition_to(ConnectionState.DISCONNECTED):
                operations += 1

            # Small delay to simulate real work
            time.sleep(0.001)

        end_time = time.perf_counter()
        with results_lock:
            results.append(
                {
                    "worker_id": worker_id,
                    "operations": operations,
                    "time": end_time - start_time,
                }
            )

    # Create multiple contending threads
    threads = []
    with _suppress_ble_debug_logs():
        start_time = time.perf_counter()
        for i in range(5):
            thread = threading.Thread(target=worker, args=(i,), daemon=True)
            threads.append(thread)
            thread.start()

        # Wait for completion
        for thread in threads:
            thread.join()
        end_time = time.perf_counter()
    total_time = end_time - start_time

    # Verify reasonable performance under contention
    assert total_time < 8.0, f"Contention test too slow: {total_time:.3f}s"

    # Verify all operations completed
    total_operations = sum(r["operations"] for r in results)
    expected_operations = 5 * 200 * 3  # 5 workers * 200 iterations * 3 operations
    assert (
        total_operations >= expected_operations * 0.8
    ), f"Too many failed operations: {total_operations}/{expected_operations}"

    logging.info(
        "Contention performance: %s operations in %.3fs",
        total_operations,
        total_time,
    )


@pytest.mark.unit
def test_memory_efficiency() -> None:
    """Verify BLEStateManager instances are collectable after repeated use.

    This avoids brittle gc.get_objects() delta checks (which vary heavily under
    coverage/plugins) and instead asserts no managers remain strongly referenced.
    """
    manager_refs = []
    manager: BLEStateManager | None = None
    with _suppress_ble_debug_logs():
        for _i in range(300):
            manager = BLEStateManager()
            manager._transition_to(ConnectionState.CONNECTING)
            manager._transition_to(ConnectionState.CONNECTED)
            manager._transition_to(ConnectionState.DISCONNECTED)
            manager_refs.append(weakref.ref(manager))

    # Remove loop variable's final strong reference and force collection.
    del manager
    gc.collect()

    alive = sum(1 for ref in manager_refs if ref() is not None)
    assert (
        alive == 0
    ), f"Potential memory leak: {alive} BLEStateManager instances still alive"

    logging.info("Memory efficiency: all %d instances collected", len(manager_refs))


@pytest.mark.unit
@pytest.mark.skipif(
    not os.getenv("RUN_PERF_TESTS"), reason="Performance tests disabled by default"
)
def test_property_access_performance() -> None:
    """Lightweight property-access performance regression guard.

    Performs repeated reads of `state`, `isConnected`, `is_closing`, and `can_connect` on a fresh BLEStateManager instance,
    computes the average access time per property, asserts that the average remains within a reasonable bound, and prints a short timing summary.
    """

    manager = BLEStateManager()

    # Measure property access performance
    iterations = 100000
    with _suppress_ble_debug_logs():
        start_time = time.perf_counter()
        for _i in range(iterations):
            # Access all properties
            _ = manager._current_state
            _ = manager._is_connected
            _ = manager._is_closing
            _ = manager._can_connect
        end_time = time.perf_counter()
    elapsed = end_time - start_time

    # Keep a broad CI-safe bound while still catching major regressions.
    avg_time = elapsed / (iterations * 4)  # 4 properties per iteration
    assert avg_time < 0.00002, f"Property access too slow: {avg_time:.9f}s"

    logging.info(
        "Property access: %d accesses in %.3fs, avg: %.9fs",
        iterations * 4,
        elapsed,
        avg_time,
    )
