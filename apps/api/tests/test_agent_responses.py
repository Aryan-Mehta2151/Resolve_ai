from app.modules.agent.graph import _tool_response

PRODUCTS_RESULT = [
    {
        "tool_name": "commerce_list_products",
        "status": "succeeded",
        "output": {
            "products": [
                {
                    "sku": "AUDIO-100",
                    "name": "Wireless Headphones",
                    "unit_price": "129.99",
                    "stock_quantity": 10,
                }
            ]
        },
    }
]


def test_product_list_tells_linked_customer_how_to_order():
    response = _tool_response(PRODUCTS_RESULT, customer_id="customer-1")
    assert response is not None
    assert 'say "Buy 2 <SKU>"' in response
    assert "Attach a customer" not in response


def test_product_list_asks_for_customer_when_none_is_linked():
    response = _tool_response(PRODUCTS_RESULT, customer_id=None)
    assert response is not None
    assert "A customer must be selected before an order can be placed." in response
    assert "Attach a customer" not in response
