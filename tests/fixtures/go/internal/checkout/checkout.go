package checkout

import (
	"errors"
	"log/slog"

	"github.com/rs/zerolog"
	"go.uber.org/zap"
)

type Service struct {
	Logger *slog.Logger
	Zap    *zap.Logger
	Zero   zerolog.Logger
}

var ErrTimeout = errors.New("inventory timed out")

func (s *Service) Charge(id int) {
	s.Logger.Info("charging order", "order_id", id)
	if err := reserve(id); err != nil {
		s.Logger.Error("payment failed", "order_id", id, "err", err)
		s.Zap.Error("payment failed (zap)", zap.Int("order_id", id), zap.Error(err))
		s.Zero.Error().Err(err).Int("order_id", id).Msg("payment failed (zerolog)")
	}
}

func reserve(id int) error {
	if id%3 == 0 {
		return ErrTimeout
	}
	return nil
}

func Price(items []int, i int) int {
	return items[i] * 2
}
