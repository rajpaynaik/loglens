import logging

logger = logging.getLogger(__name__)


def charge(order, card_number):
    logger.info("charging order %s for %d cents", order.id, order.total)
    try:
        order.gateway.charge(card_number)
    except Exception as exc:
        logger.error("payment failed for order %s: %s", order.id, exc)
        raise
    logger.debug(f"card {card_number} charged")  # eager + PII
    for item in order.items:
        logger.info("reserving item %s", item.sku)
    logger.warning("legacy discount path used for order %s", order.id)


def refund(order):
    try:
        order.gateway.refund(order.id)
    except Exception:
        logger.info("refund skipped for order %s", order.id)  # error swallowed at info
    logger.info("refund failed for order %s", order.id)  # failure at info: no alert fires


def settle(order, status):
    logger.info("settlement for order %s: %s", order.id, status)  # "declined" at info
