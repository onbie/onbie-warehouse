#!/usr/bin/env python3
"""
Setup script to create sample Shopee orders data
Run this once after installing dependencies to generate sample data
"""

import sys

def create_sample_data():
    try:
        import pandas as pd
        print("✓ Pandas found")
    except ImportError:
        print("✗ Pandas not installed. Please run: pip install -r requirements.txt")
        return False

    try:
        import openpyxl
        print("✓ OpenPyXL found")
    except ImportError:
        print("✗ OpenPyXL not installed. Please run: pip install -r requirements.txt")
        return False

    import os

    # Create data directory
    os.makedirs("data", exist_ok=True)
    print("✓ Data directory ready")

    # Create sample data
    data = {
        'order_number': ['2406001234', '2406001235', '2406001236', '2406001237', '2406001238'],
        'tracking_number': ['SG1234567890', 'SG1234567891', 'SG1234567892', 'SG1234567893', 'SG1234567894'],
        'buyer_name': ['John Tan', 'Emily Wong', 'Ahmad bin Ali', 'Maria Santos', 'Zhang Wei'],
        'province': ['Central', 'West', 'North', 'East', 'Central'],
        'city': ['Singapore', 'Jurong', 'Woodlands', 'Bedok', 'Orchard'],
        'SKU': ['SKU-001', 'SKU-002', 'SKU-001', 'SKU-003', 'SKU-002'],
        'variation': ['Red/M', 'Blue/L', 'Red/S', 'Black/XL', 'Blue/M'],
        'quantity': [2, 1, 3, 1, 2],
        'order_status': ['Ready to Ship', 'Ready to Ship', 'Processing', 'Ready to Ship', 'Processing']
    }

    df = pd.DataFrame(data)
    df.to_excel("data/shopee_orders.xlsx", index=False)
    print("✓ Sample data file created: data/shopee_orders.xlsx")

    return True

if __name__ == "__main__":
    print("\n📦 Shopee Packing Checker - Setup Script\n")
    if create_sample_data():
        print("\n✅ Setup complete! Run the app with: streamlit run app.py\n")
        sys.exit(0)
    else:
        print("\n❌ Setup failed. Please install dependencies first.\n")
        sys.exit(1)
