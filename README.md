# Shopee Packing Checker

A simple Streamlit app to manage Shopee order packing workflow.

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Run the app:
```bash
streamlit run app.py
```

The app will open in your browser at `http://localhost:8501`

## Features

- 🔍 Search orders by order number or tracking number
- 📦 View order details (buyer, location, SKU, quantity, status)
- ✅ Mark orders as packed
- 🔴 Red warning for already packed orders
- 📊 Track total, packed, and remaining orders
- 💾 Automatically saves packed order numbers to `packed.csv`

## File Structure

```
Onbie Packing Sy/
├── app.py                    # Main Streamlit app
├── requirements.txt          # Python dependencies
├── packed.csv               # Auto-generated file tracking packed orders
├── data/
│   └── shopee_orders.xlsx   # Your Shopee orders data
└── README.md
```

## Data Format

The `data/shopee_orders.xlsx` file should contain the following columns:
- `order_number` - Unique order ID
- `tracking_number` - Tracking number
- `buyer_name` - Customer name
- `province` - Province/Region
- `city` - City
- `SKU` - Product SKU
- `variation` - Product variation (e.g., "Red/M")
- `quantity` - Order quantity
- `order_status` - Current status (e.g., "Ready to Ship", "Processing")

## Usage

1. Search for an order using the search box
2. View the order details
3. Click "Mark as Packed" when done
4. The app will show "Already packed" if you search for the same order again
5. Track your progress with the statistics at the bottom
