package container

import (
	"context"
	"errors"
	"net"
	"strconv"
	"testing"
	"time"
)

func TestInstanceContainerLabelsKeepIdentityAndExplicitEmptyComposeKeys(t *testing.T) {
	req := CreateRequest{UserID: "11111111-1111-4111-8111-111111111111", ChallengeID: 3}
	instanceID := "inst_22222222222222222222222222222222"
	labels := instanceContainerLabels(req, instanceID)
	want := map[string]string{
		"ctf-challenge":              "true",
		"challenge-id":               "3",
		"user-id":                    req.UserID,
		"instance-id":                instanceID,
		"com.docker.compose.project": "",
		"com.docker.compose.service": "",
	}
	if len(labels) != len(want) {
		t.Fatalf("label count = %d, want %d", len(labels), len(want))
	}
	for key, value := range want {
		if got, exists := labels[key]; !exists || got != value {
			t.Errorf("label %q = %q (present %v), want %q explicitly present", key, got, exists, value)
		}
	}
}

func TestInstanceContainerLabelsPreventInheritedOperationalAndOwnershipValues(t *testing.T) {
	req := CreateRequest{UserID: "11111111-1111-4111-8111-111111111111", ChallengeID: 3}
	instanceID := "inst_22222222222222222222222222222222"
	for _, inherited := range []struct {
		name    string
		project string
		service string
	}{
		{name: "cached operational image", project: "ctf_web", service: "learn-service"},
		{name: "unrecognized image values", project: "unknown-project", service: "unknown-service"},
	} {
		t.Run(inherited.name, func(t *testing.T) {
			imageLabels := map[string]string{
				"ctf-challenge":              "false",
				"challenge-id":               "67",
				"user-id":                    "image-user",
				"instance-id":                "image-instance",
				"com.docker.compose.project": inherited.project,
				"com.docker.compose.service": inherited.service,
				"example.image-metadata":     "retained",
			}
			labels := instanceContainerLabels(req, instanceID)
			// Docker는 값의 공백 여부가 아니라 키 존재 여부로 이미지 라벨을 병합한다.
			for key, value := range imageLabels {
				if _, exists := labels[key]; !exists {
					labels[key] = value
				}
			}
			for key, value := range instanceContainerLabels(req, instanceID) {
				if labels[key] != value {
					t.Errorf("image value leaked into label %q: got %q, want %q", key, labels[key], value)
				}
			}
			if labels["example.image-metadata"] != "retained" {
				t.Fatal("unrelated image labels must retain existing inheritance behavior")
			}
			if imageLabels["com.docker.compose.project"] != inherited.project || imageLabels["com.docker.compose.service"] != inherited.service {
				t.Fatal("container label policy must not modify image metadata")
			}
		})
	}
}

func TestInstanceContainerLabelsDoNotLeakBetweenRequests(t *testing.T) {
	first := instanceContainerLabels(CreateRequest{UserID: "first-user", ChallengeID: 1}, "inst_11111111111111111111111111111111")
	first["com.docker.compose.project"] = "ctf_web"
	first["com.docker.compose.service"] = "learn-service"
	first["user-id"] = "changed-user"
	first["unexpected"] = "first-only"
	second := instanceContainerLabels(CreateRequest{UserID: "second-user", ChallengeID: 2}, "inst_22222222222222222222222222222222")
	if second["com.docker.compose.project"] != "" || second["com.docker.compose.service"] != "" || second["user-id"] != "second-user" || second["challenge-id"] != "2" || second["instance-id"] != "inst_22222222222222222222222222222222" {
		t.Fatal("instance labels must be independently generated for each request")
	}
	if _, exists := second["unexpected"]; exists {
		t.Fatal("labels from an earlier request must not leak into the next request")
	}
}

func TestVerificationContainerLabelsKeepIdentityAndBlockComposeInheritance(t *testing.T) {
	verificationID := "verify_33333333333333333333333333333333"
	want := map[string]string{
		"ctf-challenge":              "true",
		"challenge-id":               "3",
		"instance-id":                verificationID,
		"verification":               "true",
		"com.docker.compose.project": "",
		"com.docker.compose.service": "",
	}
	labels := verificationContainerLabels(3, verificationID)
	if len(labels) != len(want) {
		t.Fatalf("label count = %d, want %d", len(labels), len(want))
	}
	for key, value := range want {
		if got, exists := labels[key]; !exists || got != value {
			t.Errorf("label %q = %q (present %v), want %q explicitly present", key, got, exists, value)
		}
	}
	labels["user-id"] = "first-request-only"
	labels["com.docker.compose.project"] = "ctf_web"
	second := verificationContainerLabels(4, "verify_44444444444444444444444444444444")
	if _, exists := second["user-id"]; exists || second["com.docker.compose.project"] != "" || second["challenge-id"] != "4" {
		t.Fatal("verification labels must not inherit participant or previous request identity")
	}
}

func TestGlobalCapacityErrorKeepsConfiguredLimit(t *testing.T) {
	base := &GlobalCapacityError{Limit: 50}
	err := errors.Join(errors.New("port allocation failed"), base)

	var capacityErr *GlobalCapacityError
	if !errors.As(err, &capacityErr) {
		t.Fatal("GlobalCapacityError must remain detectable through wrapped errors")
	}
	if capacityErr.Limit != 50 {
		t.Fatalf("limit = %d, want 50", capacityErr.Limit)
	}
}

func TestDynamicFlagRequiredIsPublicReadinessCode(t *testing.T) {
	if got := PublicEnvironmentErrorCode("dynamic_flag_required"); got != "dynamic_flag_required" {
		t.Fatalf("public code = %q, want dynamic_flag_required", got)
	}
	if message := PublicEnvironmentErrorMessage("dynamic_flag_required"); message == "" || message == PublicEnvironmentErrorMessage("environment_unavailable") {
		t.Fatalf("dynamic flag error message was not specific: %q", message)
	}
}

func TestIsInstanceExpired(t *testing.T) {
	now := time.Now().Unix()
	tests := []struct {
		name string
		data map[string]string
		want bool
	}{
		{
			name: "future expiration is active",
			data: map[string]string{"expires_at": strconv.FormatInt(now+60, 10)},
			want: false,
		},
		{
			name: "current expiration is expired",
			data: map[string]string{"expires_at": strconv.FormatInt(now, 10)},
			want: true,
		},
		{
			name: "invalid expiration fails closed",
			data: map[string]string{"expires_at": "invalid"},
			want: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := isInstanceExpired(tt.data); got != tt.want {
				t.Fatalf("isInstanceExpired() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestFlagDependentHealthcheckDetectionIsGeneric(t *testing.T) {
	tests := []struct {
		name string
		test []string
		want bool
	}{
		{
			name: "runtime flag file check",
			test: []string{"CMD-SHELL", "test -s /flag.txt && wget -qO- 127.0.0.1:8080/health"},
			want: true,
		},
		{
			name: "runtime flag environment check",
			test: []string{"CMD-SHELL", "python -c 'assert os.environ.get(\"FLAG_PATH\")'"},
			want: true,
		},
		{
			name: "ordinary readiness check",
			test: []string{"CMD-SHELL", "wget -qO- 127.0.0.1:8080/health"},
			want: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := healthcheckDependsOnFlag(tt.test); got != tt.want {
				t.Fatalf("healthcheckDependsOnFlag(%v) = %v, want %v", tt.test, got, tt.want)
			}
		})
	}
}

func TestFlagHealthcheckFailureIsFatalOnlyForPortableHealthchecks(t *testing.T) {
	tests := []struct {
		name          string
		flagDependent bool
		dynamic       bool
		want          bool
	}{
		{name: "ordinary healthcheck remains fatal", flagDependent: false, dynamic: false, want: true},
		{name: "static legacy flag check is advisory", flagDependent: true, dynamic: false, want: false},
		{name: "dynamic flag check is advisory", flagDependent: true, dynamic: true, want: false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := flagHealthcheckFailureIsFatal(tt.flagDependent, tt.dynamic); got != tt.want {
				t.Fatalf("flagHealthcheckFailureIsFatal(%v, %v) = %v, want %v", tt.flagDependent, tt.dynamic, got, tt.want)
			}
		})
	}
}

func TestProbeContainerPortDistinguishesListeningAndUnavailableTargets(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}

	port := listener.Addr().(*net.TCPAddr).Port
	if err := probeContainerPort(context.Background(), "127.0.0.1", port); err != nil {
		listener.Close()
		t.Fatalf("probe listening port: %v", err)
	}

	if err := listener.Close(); err != nil {
		t.Fatalf("close listener: %v", err)
	}
	if err := probeContainerPort(context.Background(), "127.0.0.1", port); err == nil {
		t.Fatal("probe must reject a port after its listener closes")
	}
}
