#!/usr/bin/env python3
"""
Setup script for purna CLI tool
"""

from setuptools import setup, find_packages

setup(
    name="purna-cli",
    version="0.1.0",
    description="PurnaOS CLI - Client-driven repository knowledge builder",
    author="PurnaOS Team",
    packages=find_packages(include=["purna_cli", "purna_cli.*"]),
    install_requires=[
        "httpx>=0.27.0",
        "pyyaml>=6.0.0",
        "watchdog>=4.0.0",
    ],
    entry_points={
        "console_scripts": [
            "purna=purna_cli.cli:main",
        ],
    },
    python_requires=">=3.10",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
