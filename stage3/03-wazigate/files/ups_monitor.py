import smbus2
import time
import os
import logging
import subprocess
import sys

# Configuration
I2C_BUS = 1
I2C_ADDR = 0x42                 # I2C address
REG_VOLTAGE = 0x02              # Register where to read voltage
THRESHOLD = 6.1                 # Shutdown voltage threshold (Volts)
CHECK_INTERVAL = 10             # Seconds between checks
MAX_CONSECUTIVE_ERRORS = 5      # Stop the service if we fail to read the sensor 5 times in a row

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# Setup Bus
bus = smbus2.SMBus(I2C_BUS)

def check_hardware_presence():
    """Ping the I2C address once to verify hardware is connected."""
    try:
        bus.read_byte(I2C_ADDR)
        return True
    except OSError:
        return False

def read_voltage():
    try:
        # Read the raw 16-bit value from register 0x02
        # Note: We use read_word_data to get 2 bytes at once
        raw_data = bus.read_word_data(I2C_ADDR, REG_VOLTAGE)

        # Endianness swap: Many chips send the Low Byte first
        # This swaps the bytes to make it readable by the CPU
        raw_data = ((raw_data & 0xFF) << 8) | ((raw_data & 0xFF00) >> 8)

        # INA219 Logic:
        # The voltage is stored in the top 13 bits of the 16-bit register.
        # We shift right by 3 and multiply by 4mV (0.004)
        voltage = (raw_data >> 3) * 0.004
        return voltage
    except Exception as e:
        logging.error(f"Error reading I2C: {e}")
        return None


def main():
    logging.info("UPS Monitor Started.")

    # 1. Startup Check
    if not check_hardware_presence():
        logging.critical(f"UPS HAT not found at I2C address {hex(I2C_ADDR)}. Stopping service.")
        sys.exit(1) # Exit with error code to signal failure

    consecutive_errors = 0
    i = 0

    while True:
        try:
            voltage = read_voltage()
            consecutive_errors = 0 # Reset counter on success

            if voltage is not None:
                if i >= 6:
                    logging.info(f"Current Battery Voltage: {voltage:.2f}V")
                    i = 0
                
                if voltage < THRESHOLD:
                    logging.warning(f"CRITICAL: Low voltage ({voltage:.2f}V). Initiating shutdown sequence.")
                    
                    # 1. Stop Docker
                    try:
                        subprocess.run(["docker", "compose", "down"], cwd="/var/lib/wazigate/", check=True, capture_output=True, text=True)
                        logging.info("Docker containers stopped.")
                    except Exception as e:
                        logging.error(f"Docker cleanup failed: {e}")

                    # 2. Sync and Shutdown
                    logging.info("Syncing file system...")
                    os.system("sync")
                    
                    logging.info("Performing shutdown now...")
                    os.system("sudo shutdown -h now")
                    break

        except Exception as e:
            consecutive_errors += 1
            logging.error(f"I2C read failure ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}")
            
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                logging.critical("UPS HAT communication lost permanently. Stopping service.")
                sys.exit(1)

        i += 1
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()