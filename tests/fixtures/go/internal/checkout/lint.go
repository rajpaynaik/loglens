package checkout

import (
	"fmt"
	"log/slog"

	"github.com/rs/zerolog/log"
	"go.uber.org/zap"
)

type Service struct {
	logger *slog.Logger
	zl     *zap.Logger
}

func (s *Service) Charge(order Order, cardNumber string) error {
	s.logger.Info("charging order", "order_id", order.ID, "cents", order.Total)
	if err := s.gateway.Charge(cardNumber); err != nil {
		s.logger.Error("payment failed", "order_id", order.ID)
		return err
	}
	slog.Debug(fmt.Sprintf("card %s charged", cardNumber))
	for _, item := range order.Items {
		slog.Info("reserving item", "sku", item.SKU)
	}
	log.Warn().Str("order_id", order.ID).Msg("legacy discount path used")
	return nil
}

func (s *Service) Refund(order Order) {
	if err := s.gateway.Refund(order.ID); err != nil {
		s.logger.Info("refund skipped", "order_id", order.ID, "err", err)
	}
	slog.Info("refund failed for order " + order.ID)
	s.zl.Error("settlement failed", zap.String("order_id", order.ID), zap.Error(err))
	log.Error().
		Err(err).
		Str("email", order.Email).
		Msgf("notify %s failed", order.ID)
	fmt.Println("not a log")
}
