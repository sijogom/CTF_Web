package lifecycle

// InstanceRuntimeCleanup은 TTL watcher와 reconciler가 인스턴스 레코드를 없애기
// 직전에 호출하는 정리 hook이다. 동적 플래그처럼 인스턴스 수명과 함께 폐기해야 하는
// 런타임 비밀값을 담당하며, static/regex 인스턴스는 구현체에서 no-op으로 처리한다.
//
// data에는 Redis의 비밀 없는 인스턴스 metadata만 전달한다. raw flag나 Docker 로그는
// 이 경계를 통과하지 않는다.
type InstanceRuntimeCleanup func(instanceID string, data map[string]string, reason string)

func runInstanceRuntimeCleanup(cleanup InstanceRuntimeCleanup, instanceID string, data map[string]string, reason string) {
	if cleanup != nil {
		cleanup(instanceID, data, reason)
	}
}
