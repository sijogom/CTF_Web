// Package security는 챌린지 컨테이너의 보안 격리 정책을 정의한다.
package security

import (
	dockercontainer "github.com/docker/docker/api/types/container"
	"github.com/docker/go-units"
)

// ContainerPolicy는 챌린지 컨테이너에 적용할 보안 정책이다.
type ContainerPolicy struct {
	// CPU 제한 (나노초 단위, 예: 1e9 = 1 CPU)
	CPUQuota int64
	// CPU 주기 (기본 100000 = 100ms)
	CPUPeriod int64
	// 메모리 제한 (바이트, 예: 256*1024*1024 = 256MB)
	MemoryLimit int64
	// 메모리 + 스왑 제한
	MemorySwap int64
	// PID 최대 개수
	PidsLimit int64
	// 읽기 전용 루트 파일시스템
	ReadonlyRootfs bool
	// 허용할 Linux capabilities
	CapAdd []string
	// 제거할 Linux capabilities
	CapDrop []string
	// seccomp 프로파일 경로 (빈 문자열이면 기본 프로파일)
	SeccompProfile string
	// 네트워크 모드
	NetworkMode string
}

// DefaultPolicy는 기본 보안 정책을 반환한다.
func DefaultPolicy() ContainerPolicy {
	return ContainerPolicy{
		CPUQuota:       100000, // 1 CPU
		CPUPeriod:      100000,
		MemoryLimit:    256 << 20, // 256MB
		MemorySwap:     256 << 20, // 스왑 미사용
		PidsLimit:      100,
		ReadonlyRootfs: false,              // 일부 챌린지는 쓰기 필요
		CapAdd:         []string{"NET_RAW"}, // ping 등 네트워크 도구용
		CapDrop:        []string{"ALL"},     // 모든 capabilities 제거 후 필요한 것만 추가
		SeccompProfile: "",                  // Docker 기본 seccomp 사용
		NetworkMode:    "",                  // 독립 네트워크 생성 (manager에서 처리)
	}
}

// HighSecurityPolicy는 보안이 강화된 정책을 반환한다 (Pwn 문제 등).
func HighSecurityPolicy() ContainerPolicy {
	policy := DefaultPolicy()
	policy.MemoryLimit = 128 << 20 // 128MB
	policy.PidsLimit = 50
	policy.ReadonlyRootfs = true
	policy.CapAdd = nil // 추가 capability 없음
	return policy
}

// ToHostConfig는 ContainerPolicy를 Docker HostConfig로 변환한다.
func (p ContainerPolicy) ToHostConfig() *dockercontainer.HostConfig {
	pidsLimit := p.PidsLimit

	hc := &dockercontainer.HostConfig{
		Resources: dockercontainer.Resources{
			CPUQuota:  p.CPUQuota,
			CPUPeriod: p.CPUPeriod,
			Memory:    p.MemoryLimit,
			MemorySwap: p.MemorySwap,
			PidsLimit: &pidsLimit,
			Ulimits: []*units.Ulimit{
				{Name: "nofile", Soft: 1024, Hard: 2048},
			},
		},
		ReadonlyRootfs: p.ReadonlyRootfs,
		CapAdd:         p.CapAdd,
		CapDrop:        p.CapDrop,
		SecurityOpt:    []string{"no-new-privileges:true"},
		AutoRemove:     false,
		RestartPolicy:  dockercontainer.RestartPolicy{Name: "no"},
	}

	if p.NetworkMode != "" {
		hc.NetworkMode = dockercontainer.NetworkMode(p.NetworkMode)
	}

	return hc
}
