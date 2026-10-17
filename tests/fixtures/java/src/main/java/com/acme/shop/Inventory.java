package com.acme.shop;

class Inventory {
    void reserve(String sku) throws java.net.SocketTimeoutException {
        throw new java.net.SocketTimeoutException("inventory read timed out after 3000ms for " + sku);
    }
}
