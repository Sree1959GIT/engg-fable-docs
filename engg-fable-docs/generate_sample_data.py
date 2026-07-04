#!/usr/bin/env python3
"""generate_sample_data.py — Creates a sample BLDC Motor Controller Excel file for testing."""

import os
import pandas as pd

data = [
    # ── POWER DISTRIBUTION (Tab 1) ──
    ["BLDC_Controller","Power_Distribution","CON1","Phoenix Contact","MSTBVA 2.5/2-G","Pin_1","D1","ANODE","12V_IN"],
    ["BLDC_Controller","Power_Distribution","CON1","Phoenix Contact","MSTBVA 2.5/2-G","Pin_2","C1","NEG","GND"],
    ["BLDC_Controller","Power_Distribution","D1","Vishay","SS34","CATHODE","C1","POS","12V_PROT"],
    ["BLDC_Controller","Power_Distribution","D1","Vishay","SS34","CATHODE","U1","BAT+","V_BAT"],
    ["BLDC_Controller","Power_Distribution","C1","Murata","GRM31CR71E106KA12","NEG","U1","GND","GND_REF"],
    ["BLDC_Controller","Power_Distribution","U1","Texas Instruments","BQ76952","ALERT","CON2","Pin_1","BMS_HW_ALERT"],

    # ── CONTROL INTERFACE (Tab 2) ──
    ["BLDC_Controller","Control_Interface","U1","Texas Instruments","BQ76952","SDA","U3","I2C_SDA","I2C_Data"],
    ["BLDC_Controller","Control_Interface","U1","Texas Instruments","BQ76952","SCL","U3","I2C_SCL","I2C_Clock"],
    ["BLDC_Controller","Control_Interface","U1","Texas Instruments","BQ76952","ALERT","U3","PE0","BMS_ALERT"],
    ["BLDC_Controller","Control_Interface","U3","STMicroelectronics","STM32G474","SPI1_SCK","U2","SPI_CLK","SPI_CLK"],
    ["BLDC_Controller","Control_Interface","U3","STMicroelectronics","STM32G474","SPI1_MOSI","U2","SPI_MOSI","SPI_MOSI"],
    ["BLDC_Controller","Control_Interface","U2","Infineon","IMC300A","SPI_MISO","U3","SPI1_MISO","SPI_MISO"],
    ["BLDC_Controller","Control_Interface","U3","STMicroelectronics","STM32G474","SPI1_NSS","U2","SPI_CS","SPI_CS"],
    ["BLDC_Controller","Control_Interface","U3","STMicroelectronics","STM32G474","SPI1_NSS","R1","Pin_1","CS_PULLUP"],
    ["BLDC_Controller","Control_Interface","R1","Yageo","RC0402FR-0710KL","Pin_2","PWR_3V3","NET","3V3_SYS"],

    # ── MOTOR DRIVE (Tab 3) ──
    ["BLDC_Controller","Motor_Drive","U3","STMicroelectronics","STM32G474","PA0","U2","IN_U","PWM_U"],
    ["BLDC_Controller","Motor_Drive","U3","STMicroelectronics","STM32G474","PA1","U2","IN_V","PWM_V"],
    ["BLDC_Controller","Motor_Drive","U3","STMicroelectronics","STM32G474","PA2","U2","IN_W","PWM_W"],
    ["BLDC_Controller","Motor_Drive","U2","Infineon","IMC300A","FAULT","U3","PB0","DRV_FAULT"],
    ["BLDC_Controller","Motor_Drive","U2","Infineon","IMC300A","OUT1","M1","U","PHASE_U"],
    ["BLDC_Controller","Motor_Drive","U2","Infineon","IMC300A","OUT2","M1","V","PHASE_V"],
    ["BLDC_Controller","Motor_Drive","U2","Infineon","IMC300A","OUT3","M1","W","PHASE_W"],
    ["BLDC_Controller","Motor_Drive","R2","Yageo","RC0402FR-0710KL","Pin_1","U2","VDC","V_SENSE"],
    ["BLDC_Controller","Motor_Drive","D1","Vishay","SS34","CATHODE","U2","VDC","DC_BUS"],
]

columns = ["System_Name","Subsystem_Name","Component_ID","Make","Model","Source_Pin","Target_ID","Target_Pin","Signal_Name"]
df = pd.DataFrame(data, columns=columns)
os.makedirs("input", exist_ok=True)
with pd.ExcelWriter("input/connectivity.xlsx", engine="openpyxl") as writer:
    for subsystem in df["Subsystem_Name"].unique():
        df[df["Subsystem_Name"] == subsystem].to_excel(
            writer, sheet_name=subsystem, index=False
        )

print(f"Created: input/connectivity.xlsx")
print(f"  {len(df)} connections across {df['Subsystem_Name'].nunique()} tabs:")
for tab in df["Subsystem_Name"].unique():
    print(f"    - {tab}: {len(df[df['Subsystem_Name']==tab])} rows")
