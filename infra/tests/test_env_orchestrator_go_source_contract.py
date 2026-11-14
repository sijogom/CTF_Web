"""Source-only regression guards for the Go orchestrator.

The Windows development workspace does not include Go/gofmt.  These checks do
not replace Go compilation; they prevent the specific physical-line comment
corruption and duplicate declarations that previously hid required statements
from the compiler until Linux verification.
"""

from pathlib import Path
import re
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_ROOT = PROJECT_ROOT / "services" / "env-orchestrator"


def _balanced_go_delimiters(source: str) -> bool:
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    state = "code"
    i = 0
    while i < len(source):
        char = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if state == "line":
            if char == "\n":
                state = "code"
            i += 1
            continue
        if state == "block":
            if char == "*" and nxt == "/":
                state = "code"
                i += 2
            else:
                i += 1
            continue
        if state == "string":
            if char == "\\":
                i += 2
            elif char == '"':
                state = "code"
                i += 1
            else:
                i += 1
            continue
        if state == "rune":
            if char == "\\":
                i += 2
            elif char == "'":
                state = "code"
                i += 1
            else:
                i += 1
            continue
        if state == "raw":
            state = "code" if char == "`" else "raw"
            i += 1
            continue
        if char == "/" and nxt == "/":
            state = "line"
            i += 2
            continue
        if char == "/" and nxt == "*":
            state = "block"
            i += 2
            continue
        if char == '"':
            state = "string"
        elif char == "'":
            state = "rune"
        elif char == "`":
            state = "raw"
        elif char in pairs:
            stack.append(char)
        elif char in pairs.values():
            if not stack or pairs[stack.pop()] != char:
                return False
        i += 1
    return state == "code" and not stack


class EnvOrchestratorGoSourceContractTests(unittest.TestCase):
    def test_dockerfile_uses_pinned_module_manifests_without_build_time_tidy(self) -> None:
        dockerfile = (ENV_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertTrue((ENV_ROOT / "go.mod").is_file())
        self.assertTrue((ENV_ROOT / "go.sum").is_file())
        self.assertIn("FROM golang:1.27.2-alpine3.24 AS builder", dockerfile)
        self.assertIn("FROM alpine:3.24.2", dockerfile)
        self.assertIn("ENV GOTOOLCHAIN=local GOTELEMETRY=off", dockerfile)
        self.assertIn("COPY go.mod go.sum ./", dockerfile)
        self.assertIn("RUN go mod download", dockerfile)
        self.assertIn("go list -mod=readonly -m all", dockerfile)
        self.assertNotIn("RUN go mod tidy", dockerfile)
        self.assertIn("go build -mod=readonly", dockerfile)

    def test_go_files_have_balanced_delimiters(self) -> None:
        for path in ENV_ROOT.rglob("*.go"):
            self.assertTrue(
                _balanced_go_delimiters(path.read_text(encoding="utf-8")),
                path,
            )

    def test_comments_do_not_swallow_following_declarations(self) -> None:
        suspicious = re.compile(r"//.*(?:\t|:=|\},)")
        for path in ENV_ROOT.rglob("*.go"):
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                self.assertIsNone(
                    suspicious.search(line),
                    f"{path}:{line_number}: {line}",
                )

    def test_required_startup_and_proxy_declarations_are_standalone(self) -> None:
        required = {
            ENV_ROOT / "cmd" / "main.go": (
                "opt, err := redis.ParseURL(redisURL)",
                "docker, err := dockerclient.NewClientWithOpts(",
                "portAllocator := container.NewPortAllocator(rdb, portMin, portMax)",
            ),
            ENV_ROOT / "internal" / "auth" / "middleware.go": (
                'authHeader := c.GetHeader("Authorization")',
            ),
            ENV_ROOT / "internal" / "container" / "builder.go": (
                "buildCtx, cancel := context.WithTimeout(ctx, 5*time.Minute)",
            ),
            ENV_ROOT / "internal" / "proxy" / "http.go": (
                "var jsURLRe = regexp.MustCompile(",
                "var headTagRe = regexp.MustCompile(",
            ),
            ENV_ROOT / "internal" / "proxy" / "terminal.go": (
                "errCh := make(chan error, 2)",
                "go func() {",
            ),
        }
        for path, declarations in required.items():
            lines = path.read_text(encoding="utf-8").splitlines()
            for declaration in declarations:
                matches = [
                    line
                    for line in lines
                    if declaration in line and not line.lstrip().startswith("//")
                ]
                expected = 2 if path.name == "terminal.go" and declaration == "go func() {" else 1
                self.assertEqual(expected, len(matches), f"{path}: {declaration}")

    def test_flag_dependent_healthchecks_are_advisory_without_dynamic_runtime(self) -> None:
        manager = (
            ENV_ROOT / "internal" / "container" / "manager.go"
        ).read_text(encoding="utf-8")
        self.assertIn("func healthcheckDependsOnFlag(test []string) bool", manager)
        self.assertIn('"flag.txt"', manager)
        self.assertIn('"get(\'flag\')"', manager)
        self.assertIn(
            "allowFlagDependentHealthcheck bool",
            manager,
        )
        self.assertIn(
            "flagHealthcheckFailureIsFatal(flagDependentHealthcheck, allowFlagDependentHealthcheck)",
            manager,
        )
        self.assertIn(
            "func flagHealthcheckFailureIsFatal(flagDependent, dynamicRuntime bool) bool",
            manager,
        )
        self.assertIn("_ = dynamicRuntime", manager)
        self.assertIn("return !flagDependent", manager)
        self.assertIn(
            "dynamicFlagEnabled(data)",
            manager,
        )
        self.assertIn(
            "verificationDynamicEnabled",
            manager,
        )
        self.assertIn(
            "Static/regex",
            manager,
        )

    def test_build_context_flags_are_normalized_before_the_docker_tar(self) -> None:
        builder = (
            ENV_ROOT / "internal" / "container" / "builder.go"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'const dynamicBuildFlagPlaceholder = "CTF_DYNAMIC_FLAG_PLACEHOLDER\\n"',
            builder,
        )
        self.assertIn("normalizeBuildContextFlagFiles(files, flagType)", builder)
        self.assertIn('path.Base(path.Clean(name)) != "flag.txt"', builder)
        self.assertIn("files[name] = compatibilityFlagFileContent(dockerfileContent)", builder)
        self.assertIn("const compatibilityHealthcheckFlag = \"ok\\n\"", builder)
        self.assertIn("static/regex keep a non-secret compatibility value", builder)
        self.assertNotIn('return &EnvironmentReadinessError{Code: "invalid_environment"}', builder.split("func normalizeBuildContextFlagFiles", 1)[1].split("// createBuildContext", 1)[0])

    def test_generic_builder_accepts_flag_healthchecks_with_non_secret_compatibility_file(self) -> None:
        builder = (ENV_ROOT / "internal" / "container" / "builder.go").read_text(
            encoding="utf-8"
        )
        manager = (ENV_ROOT / "internal" / "container" / "manager.go").read_text(
            encoding="utf-8"
        )
        self.assertIn("func validateDockerfileFlagHealthcheck", builder)
        self.assertIn("func dockerfileHealthcheckDependsOnFlag", builder)
        self.assertIn("func dockerfileReferencesFlagFile", builder)
        self.assertIn("dockerfileReferencesFlagFile(string(dockerfile))", builder)
        self.assertNotIn('if flagType == "dynamic_instance" {', builder.split("func ensureCompatibilityFlagFile", 1)[1].split("func validateDockerfileFlagHealthcheck", 1)[0])
        self.assertIn("Do not reject a static/regex build solely", builder)
        validate_section = builder.split("func validateDockerfileFlagHealthcheck", 1)[1].split("func dockerfileHealthcheckDependsOnFlag", 1)[0]
        self.assertNotIn('Code: "dynamic_flag_required"', validate_section)
        self.assertIn('"dynamic_flag_required"', manager)
        self.assertIn("flag_type", builder)

    def test_admin_global_container_limit_reaches_atomic_allocator(self) -> None:
        manager = (
            ENV_ROOT / "internal" / "container" / "manager.go"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "func (m *Manager) getAdminSettings(ctx context.Context) (ttl int, maxPerUser int, maxContainers int)",
            manager,
        )
        self.assertIn("if settings.MaxContainers > 0", manager)
        self.assertIn(
            "m.portAllocator.AllocateWithinLimitForOwner(ctx, maxContainers, instanceID)",
            manager,
        )
        creation = manager.split("func (m *Manager) CreateInstance", 1)[1]
        self.assertLess(
            creation.index("endCreation := m.portAllocator.BeginCreation(instanceID)"),
            creation.index("m.portAllocator.AllocateWithinLimitForOwner"),
        )
        self.assertIn("defer endCreation()", creation)


if __name__ == "__main__":
    unittest.main()
