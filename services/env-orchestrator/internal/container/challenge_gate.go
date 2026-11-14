package container

import (
	"context"
	"sync"
)

type challengeOperationGate struct {
	readers int
	writing bool
	writers int
	refs    int
	changed chan struct{}
}

var challengeOperationGates = struct {
	sync.Mutex
	entries map[int]*challengeOperationGate
}{entries: make(map[int]*challengeOperationGate)}

// The deployed orchestrator is a single process. Builds, readiness checks and
// provisioning share this gate; committed deletion waits for all three before
// observing resources. Waiting requests can cancel without starting any work.
func acquireChallengeOperation(ctx context.Context, challengeID int, exclusive bool) (func(), error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	registry := &challengeOperationGates
	registry.Lock()
	gate := registry.entries[challengeID]
	if gate == nil {
		gate = &challengeOperationGate{changed: make(chan struct{})}
		registry.entries[challengeID] = gate
	}
	gate.refs++
	if exclusive {
		gate.writers++
	}
	for {
		if err := ctx.Err(); err != nil {
			if exclusive {
				gate.writers--
			}
			gate.refs--
			close(gate.changed)
			gate.changed = make(chan struct{})
			if gate.refs == 0 {
				delete(registry.entries, challengeID)
			}
			registry.Unlock()
			return nil, err
		}
		allowed := !gate.writing && ((exclusive && gate.readers == 0) || (!exclusive && gate.writers == 0))
		if allowed {
			if exclusive {
				gate.writers--
				gate.writing = true
			} else {
				gate.readers++
			}
			registry.Unlock()
			var once sync.Once
			return func() {
				once.Do(func() {
					registry.Lock()
					defer registry.Unlock()
					if exclusive {
						gate.writing = false
					} else {
						gate.readers--
					}
					gate.refs--
					close(gate.changed)
					gate.changed = make(chan struct{})
					if gate.refs == 0 {
						delete(registry.entries, challengeID)
					}
				})
			}, nil
		}
		changed := gate.changed
		registry.Unlock()
		select {
		case <-ctx.Done():
		case <-changed:
		}
		registry.Lock()
	}
}
