package lifecycle

import "testing"

func TestRunInstanceRuntimeCleanupPassesOnlyMetadataAndReason(t *testing.T) {
	metadata := map[string]string{
		"dynamic_flag_enabled": "true",
		"challenge_id":          "42",
	}
	called := false
	runInstanceRuntimeCleanup(func(instanceID string, data map[string]string, reason string) {
		called = true
		if instanceID != "inst_0123456789abcdef0123456789abcdef" {
			t.Fatalf("instanceID = %q", instanceID)
		}
		if data["dynamic_flag_enabled"] != "true" || reason != "instance_expired" {
			t.Fatalf("metadata/reason = %#v / %q", data, reason)
		}
	}, "inst_0123456789abcdef0123456789abcdef", metadata, "instance_expired")
	if !called {
		t.Fatal("runtime cleanup hook was not called")
	}
}

func TestRunInstanceRuntimeCleanupAllowsStaticNoop(t *testing.T) {
	runInstanceRuntimeCleanup(nil, "inst_0123456789abcdef0123456789abcdef", map[string]string{
		"dynamic_flag_enabled": "false",
	}, "reconcile_cleanup")
}
