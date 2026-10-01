# Legacy test disposition (2.4.3 coordinator suite -> rebuild)

165 legacy tests failed after the rebuild removed the 2.4.3 coordinator
internals. Each one is listed below.

- **ported**: same test name, rewritten against the rebuild's public API in the file named.
- **replaced**: an existing rebuild test protects the same behaviour.
- **new**: a test written for this disposition.
- **inapplicable**: the protected mechanism no longer exists, and no behaviour depends on it.

Where an expected value deliberately changed, the SPEC/CONTRACTS section is in the Note column.

Abbreviations:
- C = `test_controller.py`
- K = `test_rebuild_contract.py`
- A = `test_ha_adapters.py`
- L = `test_legacy_behaviours.py`
- R = `test_ha_real_controller.py`
- P = `test_persistence.py`
- S = `test_session_tracking.py`

Three **new** tests are strict `xfail`s. Each one records a controller defect found while porting, and is marked (XFAIL) in the table:

- The unplug-rearm path: SPEC 5.2 requires it, and the controller has none.
- A confirmed 0x3005 change is not rescheduled into the running refresh wait.
- A restored power above the device maximum never latches a fault.

## Counts

| Disposition | Count |
|---|---|
| ported | 82 |
| replaced | 54 |
| new | 27 |
| inapplicable | 2 |
| **total** | **165** |

## test_block_freshness.py (ported in place)

| Legacy test | Disposition |
|---|---|
| TestBlockIsFresh::test_successful_fetch_marks_status_and_config_fresh_but_not_phase_box | ported |
| TestBlockIsFresh::test_stale_block_becomes_fresh_again_once_it_succeeds | ported |
| TestBlockIsFresh::test_threshold_scales_with_scan_interval | ported |
| TestEntityAvailability::test_stale_block_only_disables_its_own_entities | ported |
| TestEntityAvailability::test_total_connection_loss_disables_everything | ported. The loss is now driven by real failed polls instead of forcing the flag. |
| TestEntityAvailability::test_stale_block_recovering_makes_its_entities_available_again | ported |

## test_capabilities.py (TestDynamicNumberBounds ported in place)

| Legacy test | Disposition |
|---|---|
| TestDynamicNumberBounds::test_max_charging_current_defaults_to_a7300_when_model_unknown | ported |
| TestDynamicNumberBounds::test_max_charging_current_follows_detected_three_phase_model | ported |
| TestDynamicNumberBounds::test_max_charging_power_follows_detected_model | ported |
| TestDynamicNumberBounds::test_non_capability_bound_number_uses_static_description_value | ported |

## test_coordinator_batching.py (ported in place, onto protocol.SnapshotReader)

| Legacy test | Disposition | Note |
|---|---|---|
| test_status_block_is_a_single_batched_fc03_request | ported | |
| test_phase_switch_box_block_is_a_distinct_separate_request | ported | |
| test_exactly_three_read_registers_calls_per_poll | ported | CONTRACTS B: once the phase box is definitively refused, it is no longer probed, so that case is 2 requests per poll after the first. |

## test_diagnostics.py (ported in place, real config-entry setup)

| Legacy test | Disposition |
|---|---|
| test_diagnostics_redacts_host_and_rfid_card | ported |
| test_diagnostics_redacts_serial_number | ported |
| test_diagnostics_includes_model_and_capabilities | ported |
| test_diagnostics_includes_transport_counters | ported. The live path is also covered by `test_transport_counters.py`. |
| test_diagnostics_includes_block_health | ported |

## test_energy_persistence.py (ported in place: EnergyTracker / ChargerStorage / HA unload)

| Legacy test | Disposition |
|---|---|
| TestRestoreConvertsWallClockToMonotonic::test_restored_baseline_lands_in_last_energy | ported |
| TestKnownIncidentValueRejectedAfterRestartWithPersistedBaseline::test_65800_is_rejected_as_the_first_live_reading | ported |
| TestKnownIncidentValueRejectedAfterRestartWithPersistedBaseline::test_without_a_persisted_baseline_the_same_value_would_be_accepted | ported |
| TestMalformedStorageDiscardedSafely::test_non_dict_entry_is_discarded | ported |
| TestMalformedStorageDiscardedSafely::test_negative_raw_is_discarded | ported |
| TestMalformedStorageDiscardedSafely::test_non_numeric_wall_ts_is_discarded | ported |
| TestMalformedStorageDiscardedSafely::test_boolean_raw_is_rejected_despite_bool_being_an_int_subclass | ported |
| TestMalformedStorageDiscardedSafely::test_one_bad_key_does_not_discard_a_good_sibling_key | ported |
| TestDebouncedDirtyFlagSaving::test_unchanged_repeated_readings_do_not_mark_dirty | ported |
| TestDebouncedDirtyFlagSaving::test_a_changed_value_marks_dirty | ported |
| TestSessionBoundaryAndUnloadFlushPromptly::test_session_boundary_marks_dirty_even_if_value_unchanged | ported. Now stricter: the baseline is seeded first, so the value really is unchanged. |
| TestSessionBoundaryAndUnloadFlushPromptly::test_unload_flushes_unconditionally | ported. The per-poll save is made to fail, then unload must persist the value. |

## test_energy_sanitization.py (ported in place)

| Legacy test | Disposition |
|---|---|
| TestSessionBoundaryCrossReference::test_decrease_coinciding_with_a_real_session_start_is_accepted | ported. Goes through the coordinator, so the boundary comes from its own session tracking. |
| TestSessionBoundaryCrossReference::test_decrease_without_a_session_transition_is_rejected | ported |
| TestSessionBoundaryCrossReference::test_decrease_to_zero_is_still_accepted_without_a_confirmed_boundary | ported |
| TestFirstObservationAbsoluteBound::test_corrupt_first_reading_for_a_key_is_rejected | ported |
| TestSustainedWindowCorruption::test_real_intermittent_charging_never_trips_the_window | ported |
| TestSustainedWindowCorruption::test_sustained_one_quantum_per_poll_is_eventually_rejected | ported |
| TestSustainedWindowCorruption::test_window_resets_after_a_rejection_so_it_can_recover | ported. Asserts the window directly, because there is no public view of it. |
| TestSessionBoundaryDoesNotPoisonTheWindow::test_session_reset_clears_and_reseeds_the_window | ported. Behavioural: after the reset, the tracker trips after exactly as many polls as a fresh tracker. |
| TestSessionBoundaryDoesNotPoisonTheWindow::test_reset_does_not_mask_a_later_corrupt_spike | ported |
| TestWindowStoresRawObservationsNotDeltas::test_window_entries_are_timestamp_raw_pairs | ported. Asserts the window directly. |
| TestRejectionNeverBecomesTheNewAnchor::test_per_poll_rejection_leaves_last_energy_and_window_untouched | ported |
| TestRejectionNeverBecomesTheNewAnchor::test_two_consecutive_corrupt_reads_both_compare_against_the_same_anchor | ported |
| TestFullThirtyMinuteQuantisedTraceAtRatedPower::test_every_legitimate_sample_over_30_minutes_is_accepted[0.0] | ported |
| TestFullThirtyMinuteQuantisedTraceAtRatedPower::test_every_legitimate_sample_over_30_minutes_is_accepted[0.025] | ported |
| TestFullThirtyMinuteQuantisedTraceAtRatedPower::test_every_legitimate_sample_over_30_minutes_is_accepted[0.05] | ported |
| TestFullThirtyMinuteQuantisedTraceAtRatedPower::test_every_legitimate_sample_over_30_minutes_is_accepted[0.075] | ported |
| TestFullThirtyMinuteQuantisedTraceAtRatedPower::test_every_legitimate_sample_over_30_minutes_is_accepted[0.099] | ported |
| TestSustainedArtificialCorruptionThenRecovery::test_exactly_one_quantum_every_10s_is_eventually_rejected | ported |
| TestSustainedArtificialCorruptionThenRecovery::test_recovers_once_the_corruption_stops | ported |

## test_protocol.py

| Legacy test | Disposition |
|---|---|
| test_snapshot_keys_match_legacy_coordinator | ported. It compares against a frozen `tests/legacy_243_snapshot.py`, captured by running the 4490e4a coordinator unmodified over the same `RegisterMap`. |

## test_session_persistence.py (ported in place: coordinator + real ChargerStorage, SessionTracker)

| Legacy test | Disposition |
|---|---|
| TestPersistingOnSessionTransitions::test_session_start_triggers_a_save_with_start_baseline | ported. The first idle poll may now persist `prev_status`, but it must not record a session start. |
| TestRestoringAfterRestart::test_mid_session_restart_preserves_start_baseline | ported (SessionTracker with manual clocks) |
| TestRestoringAfterRestart::test_last_completed_session_survives_restart | ported |
| TestRestoringAfterRestart::test_no_stored_state_is_a_safe_no_op | ported |
| TestRestoringAfterRestart::test_no_store_configured_is_a_safe_no_op | ported |
| TestPrevStatusRestorationBug::test_restart_mid_session_preserves_baseline_not_reset | ported |
| TestPrevStatusRestorationBug::test_restart_while_inactive_creates_no_phantom_session | ported |
| TestPrevStatusRestorationBug::test_genuine_new_session_after_restoration_still_starts_normally | ported |
| TestPrevStatusRestorationBug::test_prev_status_is_included_in_persisted_payload | ported |
| TestSessionCompletedEvent::test_genuine_session_end_fires_the_event | ported |
| TestSessionCompletedEvent::test_restoring_persisted_last_session_does_not_fire | ported |
| TestSessionCompletedEvent::test_two_consecutive_sessions_with_identical_duration_both_fire | ported |
| TestSessionCompletedEvent::test_no_event_without_entry_id | ported |

## test_setpoint_persistence.py (ported in place: ChargerStorage legacy import, HA number path)

| Legacy test | Disposition | Note |
|---|---|---|
| TestRestoringAfterRestart::test_restores_desired_setpoints_from_store | ported | |
| TestRestoringAfterRestart::test_no_stored_state_is_a_safe_no_op | ported | |
| TestRestoringAfterRestart::test_no_store_configured_is_a_safe_no_op | ported | The controller runs with persist=None. |
| TestPersistingOnWrite::test_dirty_flag_triggers_a_save_on_the_next_update_cycle | ported | The save now happens as part of the command, before any write (see also C::test_intent_persisted_before_the_write_is_dispatched). |
| TestPersistingOnWrite::test_not_dirty_does_not_trigger_a_save | ported | |
| TestNumberEntityMarksDirty::test_setting_a_reasserted_register_marks_setpoints_dirty | ported | |
| TestValidatingAgainstDetectedCapabilities::test_in_range_restored_value_is_left_alone | ported | |
| TestValidatingAgainstDetectedCapabilities::test_out_of_range_current_falls_back_to_device_default_not_maximum | ported | SPEC 6.1: the value is dropped and restore is protective. It no longer falls back to the default current. |
| TestValidatingAgainstDetectedCapabilities::test_out_of_range_power_with_no_safe_default_is_discarded | ported | SPEC 6.1: protective. Model-specific bounds are not applied on restore (see L XFAIL below). |
| TestValidatingAgainstDetectedCapabilities::test_out_of_range_current_with_no_default_available_is_discarded | ported | SPEC 6.1 |
| TestValidatingAgainstDetectedCapabilities::test_no_desired_setpoints_is_a_no_op | ported | |

## test_realistic_charger.py (deleted)

| Legacy test | Disposition | Note |
|---|---|---|
| test_drift_is_detected_and_resent | new: L::test_externally_reverted_cap_is_restored_within_one_refresh | The drift counter and warning are gone. SPEC 6.4/6.10 replaces drift detection with an unconditional refresh at 30 s or less. |
| test_no_drift_warning_when_not_charging | replaced: K::test_off_then_staging_never_sends_positive_power[False] | SPEC 5.2: while paused the controller keeps writing a confirmed zero, and never a positive value. |
| test_failed_heartbeat_write_is_retried_fast | replaced: C::test_failed_refresh_retries_at_retry_interval | |
| test_heartbeat_keeps_capping_with_stale_config_block | new: L::test_failing_config_block_does_not_stop_cap_refresh | |
| test_heartbeat_keeps_capping_with_stale_status_block | replaced: C::test_single_lost_read_mid_charge_does_not_interrupt | SPEC 6.9: a sustained outage now forces a protective zero (C::test_sustained_telemetry_outage_forces_protective_zero). |
| test_heartbeat_keeps_capping_during_non_fatal_alarm | new: L::test_non_fatal_alarm_keeps_cap_refreshed | |
| test_hard_fault_sends_stop_instead_of_going_silent | new: L::test_hard_fault_mid_session_keeps_refreshing_never_goes_silent | CONTRACTS C / SPEC 5.2: 0x4001 Stop is only the zero-pause fallback. "Not silent" is kept. |
| test_setup_mid_session_writes_cap_immediately | new: R::test_setup_mid_session_applies_saved_cap (+ L::test_restart_mid_session_applies_saved_cap_during_initialize) | |
| test_malformed_restored_setpoint_is_dropped[73] | ported: test_setpoint_persistence.py::test_malformed_restored_setpoint_is_dropped["73"] | SPEC 6.1: protective pause |
| test_malformed_restored_setpoint_is_dropped[None] | ported: test_setpoint_persistence.py::test_malformed_restored_setpoint_is_dropped[None] | |
| test_malformed_restored_setpoint_is_dropped[7.3] | ported: test_setpoint_persistence.py::test_malformed_restored_setpoint_is_dropped[7.3] | SPEC 6.1 |
| test_malformed_restored_setpoint_is_dropped[True] | ported: test_setpoint_persistence.py::test_malformed_restored_setpoint_is_dropped[True] | SPEC 6.1 |
| test_malformed_restored_setpoint_is_dropped[bad4] | ported: test_setpoint_persistence.py::test_malformed_restored_setpoint_is_dropped[bad4] | SPEC 6.1 |
| test_garbage_max_power_raw_does_not_disable_energy_guard | ported: test_energy_sanitization.py::test_garbage_max_power_raw_does_not_disable_energy_guard | |

## test_command_lock.py (deleted)

| Legacy test | Disposition | Note |
|---|---|---|
| TestHeartbeatThenStop::test_heartbeat_write_finishes_before_stop_is_written | replaced: K::test_pause_supersedes_queued_positive_work | |
| TestStopThenQueuedHeartbeat::test_stale_heartbeat_write_aborts_after_stop_holds_the_lock_first | replaced: C::test_old_refresh_cannot_overwrite_newer_lower_power; K::test_in_flight_refresh_cannot_overwrite_newer_lower_power | |
| TestUnloadWaitsForInFlightTransaction::test_stop_heartbeat_blocks_until_a_held_lock_is_released | replaced: K::test_close_drains_in_flight_write_before_closing_io | |
| TestUserSetpointWriteThenStop::test_setpoint_write_finishes_before_a_concurrent_stop_is_written | replaced: C::test_queued_command_overtaken_by_pause_reports_superseded | CONTRACTS C: an overtaken command reports `superseded`, not success. |
| TestStopThenUserSetpointWrite::test_setpoint_write_is_saved_but_never_reaches_the_wire_after_a_stop_that_holds_the_lock_first | replaced: K::test_off_then_staging_never_sends_positive_power[False]; C::test_limits_only_stage_while_paused | |
| TestStartAndStopOverlap::test_a_stop_landing_during_an_in_flight_start_prevents_desired_from_being_set | new: L::test_pause_during_in_flight_enable_wins | |
| TestStartAndStopOverlap::test_start_ack_sets_desired_but_waits_for_status_before_clearing_inhibit | replaced: C::test_enable_fails_visibly_when_no_session_starts; K::test_ignored_write_never_confirms_enable | |
| TestNumberEntityRoutesReassertedRegistersThroughTheLock::test_reasserted_register_calls_coordinator_method_not_the_direct_client | replaced: A::test_power_confirmed_while_enabled; A::test_current_routes_to_set_current_and_stages | |
| TestNumberEntityRoutesReassertedRegistersThroughTheLock::test_non_reasserted_register_still_writes_directly_bypassing_the_coordinator | replaced: A::test_other_numbers_route_to_set_register | CONTRACTS C: every write now goes through the controller allowlist. |
| TestReassertedRegisterSkippedWhenNotCharging::test_setpoint_saved_but_not_written_while_stopped | replaced: A::test_power_staged_while_paused_reports_desired_vs_observed; K::test_off_then_staging_never_sends_positive_power[False] | |
| TestReassertedRegisterSkippedWhenNotCharging::test_the_incident_scenario_end_to_end_cannot_resume_charging | new: R::test_full_power_number_while_paused_never_resumes | |

Not among the 165: TestPollDrivenWriterIsGone (2 tests) passed only vacuously against a MagicMock controller. Replaced by test_coordinator_batching.py::test_polling_never_issues_a_command.

## test_entity_write_reliability.py (deleted)

| Legacy test | Disposition | Note |
|---|---|---|
| test_charging_switch_turn_on_raises_on_failed_write | new: R::test_turn_on_that_cannot_be_confirmed_raises (+ A::test_control_error_surfaces_as_homeassistant_error) | SPEC 3/5.2: no compensating 0x4001 Stop. |
| test_exception_during_start_sends_compensating_stop | replaced: K::test_lost_reply_or_short_delay_confirmed_by_fresh_read[lost_reply-0] | SPEC 6.8: a lost reply is settled by a fresh read. There is no Start and no compensating Stop. |
| test_false_start_ack_sends_compensating_stop | replaced: K::test_ignored_write_never_confirms_enable | SPEC 6.8 |
| test_charging_switch_turn_on_does_not_set_desired_flag_on_failed_write | new: L::test_failed_enable_latches_and_holds_zero_until_explicit_enable[12290-ignored] | |
| test_charging_switch_programs_staged_caps_before_start | replaced: C::test_enable_resumes_saved_power_and_current_never_start | |
| test_failed_prestart_cap_aborts_start | new: L::test_failed_enable_latches_and_holds_zero_until_explicit_enable[12289-refuse] | |
| test_exception_during_prestart_cap_sends_stop | new: L::test_failed_enable_latches_and_holds_zero_until_explicit_enable[12289-lost_request] | |
| test_charging_switch_turn_on_sets_desired_flag_only_after_successful_write | replaced: A::test_turn_on_routes_to_enable; K::test_enable_completes_by_power_without_start_or_stop | |
| test_charging_switch_turn_off_clears_flag_on_successful_write | replaced: C::test_pause_writes_zero_power_and_confirms_without_0x4001 | |
| test_charging_switch_turn_off_does_not_restore_flag_on_failed_write | replaced: K::test_invalid_status_never_confirms_pause; K::test_refused_stop_fallback_fails_visibly_and_stays_off | |
| test_charging_switch_turn_off_increments_generation_before_the_write | replaced: C::test_intent_persisted_before_the_write_is_dispatched | |
| test_charging_switch_turn_on_warns_when_status_never_reflects_it | replaced: C::test_enable_fails_visibly_when_no_session_starts | Now an error, not a warning. |
| test_charging_switch_turn_on_no_warning_when_status_matches | replaced: K::test_enable_completes_by_power_without_start_or_stop | |
| test_failed_status_refresh_does_not_confirm_start_or_clear_pending_stop | replaced: K::test_unreadable_device_never_confirms_enable; C::test_read_failure_is_uncertain_not_stopped | |
| test_concurrent_stop_during_start_confirmation_keeps_stop_authoritative | new: L::test_pause_during_in_flight_enable_wins | |
| test_number_raises_on_failed_write | replaced: A::test_number_control_error_is_homeassistant_error; K::test_ignored_write_never_confirms_enable | |
| test_number_warns_on_read_back_mismatch | replaced: C::test_ack_without_application_never_confirms | Now an error, not a warning. |
| test_work_mode_select_raises_on_failed_write | replaced: A::test_work_mode_select_error | |

## test_heartbeat_task.py (deleted)

| Legacy test | Disposition | Note |
|---|---|---|
| TestIntervalUsage::test_loop_computes_interval_from_current_time_validity[10] | replaced: C::test_refresh_is_half_of_a_short_validity | |
| TestIntervalUsage::test_loop_computes_interval_from_current_time_validity[60] | replaced: C::test_safety_configuration_written_and_read_back | This test asserts refresh_interval 30. |
| TestIntervalUsage::test_loop_computes_interval_from_current_time_validity[180] | replaced: C::test_refresh_every_30s_even_when_validity_reports_180 | |
| TestRescheduleOnTimeValidityChange::test_mid_wait_change_wakes_early_and_reschedules | new: L::test_shortened_validity_takes_effect_before_the_cap_can_lapse (XFAIL) | Controller defect |
| TestImmediatePush::test_charging_start_pushes_the_desired_setpoint_immediately | replaced: C::test_enable_resumes_saved_power_and_current_never_start | |
| TestImmediatePush::test_setpoint_change_pushes_immediately_while_charging | replaced: C::test_current_ceiling_written_with_power_when_enabled; K::test_full_session | |
| TestStopRace::test_turn_off_clears_desired_flag_before_a_tick_can_repush | replaced: K::test_pause_supersedes_queued_positive_work | SPEC 5.2: a pause is a confirmed zero, and no Stop is retried. |
| TestFreshnessGate::test_stale_config_block_still_pushes | new: L::test_failing_config_block_does_not_stop_cap_refresh | |
| TestFreshnessGate::test_stale_status_block_still_pushes_on_last_known_active_status | replaced: C::test_single_lost_read_mid_charge_does_not_interrupt | SPEC 6.9: a sustained outage gives a protective zero. |
| TestFreshnessGate::test_active_fault_sends_stop_instead_of_a_push | new: L::test_hard_fault_mid_session_keeps_refreshing_never_goes_silent | CONTRACTS C |
| TestFreshnessGate::test_active_alarm_still_pushes | new: L::test_non_fatal_alarm_keeps_cap_refreshed | |
| TestFreshnessGate::test_no_fault_or_alarm_allows_a_push_with_both_blocks_fresh | replaced: K::test_cap_held_for_ten_minutes_with_lost_replies[False-180-60] | |
| TestGenerationTokenRace::test_write_that_passed_its_pre_write_check_still_applies_even_if_state_changes_during_the_await | replaced: K::test_in_flight_refresh_cannot_overwrite_newer_lower_power | |
| TestGenerationTokenRace::test_stop_landing_before_a_later_register_prevents_that_writes_start | new: L::test_pause_between_current_and_power_refresh_writes_blocks_positive_power | |
| TestDictMutationDuringIteration::test_desired_setpoints_mutated_mid_tick_does_not_crash | inapplicable | Intent is an immutable record, so there is no shared dict to iterate. A concurrent change during an in-flight refresh is covered by K::test_in_flight_refresh_cannot_overwrite_newer_lower_power. |
| TestUnloadCancellation::test_stop_heartbeat_cancels_and_awaits_the_task | replaced: C::test_close_drains_in_flight_work_before_closing_io; K::test_close_drains_in_flight_write_before_closing_io | |
| TestUnloadCancellation::test_stop_heartbeat_is_a_safe_no_op_when_never_started | new: L::test_close_without_start_is_safe | |
| TestUnloadCancellation::test_unload_entry_stops_heartbeat_before_disconnecting_client | replaced: A::test_setup_initializes_starts_and_unload_closes; K::test_close_drains_in_flight_write_before_closing_io | |
| TestWriteFailureResilience::test_failed_write_is_logged_and_does_not_raise | new: L::test_unexpected_write_exceptions_do_not_end_the_refresh_loop (+ C::test_failed_refresh_retries_at_retry_interval) | |
| TestWriteFailureResilience::test_crashing_write_does_not_kill_the_background_task | new: L::test_unexpected_write_exceptions_do_not_end_the_refresh_loop | |
| TestLoopSurvivesUnexpectedErrors::test_a_raising_tick_does_not_end_the_loop | new: L::test_unexpected_write_exceptions_do_not_end_the_refresh_loop | |
| TestLoopSurvivesUnexpectedErrors::test_a_raising_interval_calculation_does_not_end_the_loop | new: L::test_nonsensical_validity_register_keeps_refreshing | |
| TestLoopSurvivesUnexpectedErrors::test_cancellation_still_works_during_the_error_backoff | new: L::test_close_during_failure_backoff_returns_promptly | |

## test_natural_session_heartbeat_protection.py (deleted)

| Legacy test | Disposition | Note |
|---|---|---|
| TestFetchSetsDesiredFlag::test_fresh_active_status_sets_charging_desired_without_any_switch_call | replaced: C::test_first_install_adopts_only_an_active_session | |
| TestFetchSetsDesiredFlag::test_inactive_status_does_not_set_the_flag | replaced: C::test_first_install_adopts_only_an_active_session | |
| TestFetchSetsDesiredFlag::test_already_desired_stays_desired_on_a_subsequent_active_poll | replaced: L::test_naturally_started_session_is_capped_from_its_first_instant | |
| TestFetchSetsDesiredFlag::test_failed_status_block_read_does_not_set_the_flag_from_stale_data | new: L::test_first_install_with_unreadable_status_does_not_authorize_charging | |
| TestFetchSetsDesiredFlag::test_does_not_clear_an_already_desired_flag | replaced: C::test_enable_while_unplugged_is_staged_and_stays_on | |
| TestUpdateDataWakesHeartbeatOnNaturalStart::test_transition_to_desired_wakes_the_heartbeat | new: L::test_naturally_started_session_is_capped_from_its_first_instant | |
| TestUpdateDataWakesHeartbeatOnNaturalStart::test_no_transition_does_not_wake_the_heartbeat_for_this_reason | inapplicable | There is no wake mechanism. The refresh is periodic and unconditional (SPEC 6.4, 6.10). |

## test_stop_inhibit.py (deleted)

| Legacy test | Disposition | Note |
|---|---|---|
| TestNaturalStartSuppressedWhileInhibited::test_active_status_does_not_set_desired_while_inhibited | replaced: K::test_off_then_staging_never_sends_positive_power[True]; C::test_implicit_resume_while_off_is_corrected | |
| TestNaturalStartSuppressedWhileInhibited::test_active_status_sets_desired_normally_once_not_inhibited | new: L::test_naturally_started_session_is_capped_from_its_first_instant | |
| test_charging_switch_state_is_session_active_across_vehicle_pause | ported: test_charging_switch_state.py | |
| TestDisconnectClearsInhibit::test_vehicle_unplugging_clears_the_inhibit | new: L::test_user_pause_then_unplug_replug_allows_new_external_session (XFAIL) | Controller defect, SPEC 5.2 |
| TestDisconnectClearsInhibit::test_staying_plugged_in_does_not_clear_the_inhibit | replaced: K::test_off_then_staging_never_sends_positive_power[True] | |
| TestDisconnectClearsInhibit::test_disconnect_marks_session_state_dirty_for_prompt_persistence | replaced: S::test_unplug_rearms_after_user_pause | |
| TestRestartMidResumeStaysInhibited::test_active_status_at_startup_does_not_arm_desired_while_inhibited | replaced: K::test_off_then_staging_never_sends_positive_power[True]; C::test_saved_off_wins_over_active_session | |
| TestStopPendingRetry::test_failed_stop_is_retried_without_any_setpoint_write | replaced: K::test_ignored_zero_pause_falls_back_to_stop_and_latches[lost_reply]; C::test_stop_fallback_suppresses_all_cap_writes_until_explicit_enable | |
| TestStopPendingRetry::test_stop_exception_becomes_retryable_failure | replaced: C::test_failed_stop_latches_fault_and_raises; K::test_refused_stop_fallback_fails_visibly_and_stays_off | |
| TestStopPendingRetry::test_only_fresh_inactive_status_clears_stop_pending | replaced: C::test_invalid_status_never_confirms_stop | |
| TestStopPendingRetry::test_active_status_does_not_clear_stop_pending | replaced: K::test_refused_stop_fallback_fails_visibly_and_stays_off | |
| TestStopPendingRetry::test_failed_status_read_does_not_clear_stop_pending_from_cached_inactive | replaced: C::test_read_failure_is_uncertain_not_stopped | |
| TestStopPendingRetry::test_active_status_at_startup_arms_desired_normally_when_not_inhibited | replaced: C::test_first_install_adopts_only_an_active_session; K::test_full_session | |
| TestStopInhibitPersistsAcrossARestart::test_inhibit_survives_a_restore_round_trip | replaced: P::test_legacy_stop_intent_imports_as_paused[True-False-False]; P::test_new_state_round_trips_with_revision_and_latches | |
| TestStopInhibitPersistsAcrossARestart::test_absent_key_defaults_to_not_inhibited | ported: test_setpoint_persistence.py::TestLegacyStopIntent::test_absent_key_defaults_to_not_inhibited | |
| TestStopInhibitPersistsAcrossARestart::test_persist_includes_the_current_inhibit_state | replaced: P::test_paused_state_projects_protective_legacy_files | |
| TestStopInhibitPersistsAcrossARestart::test_pending_stop_is_persisted_and_restored | replaced: P::test_legacy_stop_intent_imports_as_paused[False-True-True]; P::test_new_state_round_trips_with_revision_and_latches | |

## test_time_validity_firmware_cap.py (the behavioural test was removed; its unit tests stay)

| Legacy test | Disposition |
|---|---|
| test_limit_never_reverts_over_five_minutes_with_0x3005_at_180 | replaced: K::test_cap_held_for_ten_minutes_with_lost_replies[False-180-60] / [True-180-60]; C::test_refresh_every_30s_even_when_validity_reports_180 |
