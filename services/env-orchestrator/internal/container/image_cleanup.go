package container

import (
	"context"
	"errors"
	"fmt"

	dockertypes "github.com/docker/docker/api/types"
	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/filters"
	"github.com/docker/docker/errdefs"
)

var ErrChallengeImageInUse = errors.New("challenge image is still referenced by a container")

// ChallengeImageCleanupResult는 문제 삭제 시 image tag 정리 결과를 나타낸다.
// Docker 내부 경로·ID·로그는 API 응답에 포함하지 않는다.
type ChallengeImageCleanupResult struct {
	ChallengeID    int    `json:"challenge_id"`
	ImageReference string `json:"image_reference"`
	Status         string `json:"status"` // removed, absent
}

// CleanupChallengeImage는 해당 문제의 정확한 image tag만 정리한다.
// 관련 인스턴스가 하나라도 있으면 호출자가 DB 삭제를 진행하지 않도록
// ErrChallengeImageInUse을 반환한다. 다른 challenge tag나
// dangling layer는 이 경로에서 건드리지 않는다.
func (m *Manager) CleanupChallengeImage(ctx context.Context, challengeID int) (ChallengeImageCleanupResult, error) {
	if challengeID <= 0 {
		return ChallengeImageCleanupResult{}, fmt.Errorf("invalid challenge id")
	}

	imageReference := fmt.Sprintf("ctf-challenge-%d:latest", challengeID)
	containerFilters := filters.NewArgs()
	containerFilters.Add("label", fmt.Sprintf("challenge-id=%d", challengeID))
	containers, err := m.docker.ContainerList(ctx, container.ListOptions{
		All:     true,
		Filters: containerFilters,
	})
	if err != nil {
		return ChallengeImageCleanupResult{}, fmt.Errorf("challenge container lookup failed: %w", err)
	}
	if len(containers) > 0 {
		return ChallengeImageCleanupResult{}, ErrChallengeImageInUse
	}

	if _, _, err := m.docker.ImageInspectWithRaw(ctx, imageReference); err != nil {
		if errdefs.IsNotFound(err) {
			return ChallengeImageCleanupResult{
				ChallengeID:    challengeID,
				ImageReference: imageReference,
				Status:         "absent",
			}, nil
		}
		return ChallengeImageCleanupResult{}, fmt.Errorf("challenge image lookup failed: %w", err)
	}

	if _, err := m.docker.ImageRemove(ctx, imageReference, dockertypes.ImageRemoveOptions{
		Force:         false,
		PruneChildren: false,
	}); err != nil {
		if errdefs.IsNotFound(err) {
			return ChallengeImageCleanupResult{
				ChallengeID:    challengeID,
				ImageReference: imageReference,
				Status:         "absent",
			}, nil
		}
		return ChallengeImageCleanupResult{}, fmt.Errorf("challenge image removal failed: %w", err)
	}

	return ChallengeImageCleanupResult{
		ChallengeID:    challengeID,
		ImageReference: imageReference,
		Status:         "removed",
	}, nil
}
