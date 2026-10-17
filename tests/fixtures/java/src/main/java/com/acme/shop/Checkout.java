package com.acme.shop;

import java.io.PrintWriter;
import java.io.StringWriter;
import java.time.Instant;

public class Checkout {
    static final Inventory inventory = new Inventory();

    static void charge(int orderId) {
        try {
            inventory.reserve("SKU-" + orderId);
        } catch (java.net.SocketTimeoutException e) {
            throw new IllegalStateException("could not reserve stock for order " + orderId, e);
        }
    }

    static int price(String currency) {
        java.util.Map<String, Integer> rates = java.util.Map.of("USD", 100);
        return rates.get(currency) * 3;  // NullPointerException for unknown currencies
    }

    static void emit(String message, int line, Throwable t, int i) {
        StringWriter sw = new StringWriter();
        t.printStackTrace(new PrintWriter(sw));
        String ts = Instant.now().minusSeconds(60L * i % 3600).toString();
        String json = String.format(
            "{\"@timestamp\":\"%s\",\"level\":\"ERROR\",\"message\":\"%s\",\"logger_name\":\"com.acme.shop.Checkout\","
            + "\"caller_class_name\":\"com.acme.shop.Checkout\",\"caller_file_name\":\"Checkout.java\",\"caller_line_number\":%d,"
            + "\"trace_id\":\"%032x\",\"stack_trace\":\"%s\"}",
            ts, message, line, i, sw.toString().replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n").replace("\t", "\\t"));
        System.out.println(json);
    }

    public static void main(String[] args) {
        for (int i = 0; i < 40; i++) {
            try {
                charge(i);
            } catch (IllegalStateException e) {
                emit("payment failed for order " + i, 47, e, i);
            }
        }
        for (int i = 0; i < 7; i++) {
            try {
                price(i % 2 == 0 ? "EUR" : "GBP");
            } catch (NullPointerException e) {
                emit("pricing failed", 54, e, i);
            }
        }
    }
}
