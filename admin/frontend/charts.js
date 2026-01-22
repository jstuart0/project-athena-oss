/**
 * Athena Charts - Chart.js Utilities
 *
 * Provides sparklines, histograms, and other chart types
 * for the Mission Control dashboard.
 *
 * Requires Chart.js to be loaded first.
 */
(function(Athena) {
    'use strict';

    if (!Athena) {
        console.error('[AthenaCharts] Athena namespace not found');
        return;
    }

    // Default chart colors matching design system
    const COLORS = {
        primary: '#3b82f6',
        primaryLight: '#60a5fa',
        success: '#22c55e',
        successLight: '#4ade80',
        warning: '#f59e0b',
        warningLight: '#fbbf24',
        error: '#ef4444',
        errorLight: '#f87171',
        purple: '#8b5cf6',
        purpleLight: '#a78bfa',
        gray: '#6b7280',
        grayLight: '#9ca3af',
        background: 'rgba(59, 130, 246, 0.1)',
        grid: '#333333',
        text: '#9ca3af'
    };

    // Default chart configuration for dark mode
    const DEFAULT_OPTIONS = {
        responsive: true,
        maintainAspectRatio: false,
        animation: {
            duration: 300
        },
        plugins: {
            legend: {
                display: false
            },
            tooltip: {
                enabled: true,
                backgroundColor: '#1a1a1a',
                borderColor: '#333',
                borderWidth: 1,
                titleColor: '#e5e7eb',
                bodyColor: '#9ca3af',
                padding: 8,
                cornerRadius: 4
            }
        },
        scales: {
            x: {
                display: false,
                grid: {
                    display: false
                }
            },
            y: {
                display: false,
                grid: {
                    display: false
                },
                beginAtZero: true
            }
        }
    };

    /**
     * Create a sparkline chart.
     * @param {HTMLCanvasElement} canvas - Canvas element
     * @param {Object} options - Chart options
     * @param {number[]} options.data - Data points
     * @param {string} [options.color] - Line color
     * @param {boolean} [options.fill] - Whether to fill under the line
     * @returns {Chart|null} Chart instance
     */
    Athena.charts.createSparkline = function(canvas, options = {}) {
        if (!window.Chart) {
            console.warn('[AthenaCharts] Chart.js not loaded');
            return null;
        }

        const {
            data = [],
            color = COLORS.primary,
            fill = true,
            tension = 0.4
        } = options;

        const ctx = canvas.getContext('2d');

        // Create gradient for fill
        const gradient = ctx.createLinearGradient(0, 0, 0, canvas.height);
        gradient.addColorStop(0, color + '33'); // 20% opacity
        gradient.addColorStop(1, color + '00'); // 0% opacity

        return new Chart(ctx, {
            type: 'line',
            data: {
                labels: data.map((_, i) => i),
                datasets: [{
                    data: data,
                    borderColor: color,
                    borderWidth: 2,
                    backgroundColor: fill ? gradient : 'transparent',
                    fill: fill,
                    tension: tension,
                    pointRadius: 0,
                    pointHoverRadius: 3,
                    pointHoverBackgroundColor: color,
                    pointHoverBorderColor: '#fff',
                    pointHoverBorderWidth: 2
                }]
            },
            options: {
                ...DEFAULT_OPTIONS,
                plugins: {
                    ...DEFAULT_OPTIONS.plugins,
                    tooltip: {
                        ...DEFAULT_OPTIONS.plugins.tooltip,
                        callbacks: {
                            label: (context) => context.parsed.y.toString()
                        }
                    }
                }
            }
        });
    };

    /**
     * Create a histogram/bar chart.
     * @param {HTMLCanvasElement} canvas - Canvas element
     * @param {Object} options - Chart options
     * @param {string[]} options.labels - X-axis labels
     * @param {number[]} options.values - Data values
     * @param {string} [options.color] - Bar color
     * @returns {Chart|null} Chart instance
     */
    Athena.charts.createHistogram = function(canvas, options = {}) {
        if (!window.Chart) {
            console.warn('[AthenaCharts] Chart.js not loaded');
            return null;
        }

        const {
            labels = [],
            values = [],
            color = COLORS.primary
        } = options;

        const ctx = canvas.getContext('2d');

        return new Chart(ctx, {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [{
                    data: values,
                    backgroundColor: color + '80', // 50% opacity
                    borderColor: color,
                    borderWidth: 1,
                    borderRadius: 4,
                    barThickness: 'flex',
                    maxBarThickness: 50
                }]
            },
            options: {
                ...DEFAULT_OPTIONS,
                scales: {
                    x: {
                        display: true,
                        grid: {
                            display: false,
                            drawBorder: false
                        },
                        ticks: {
                            color: COLORS.text,
                            font: {
                                size: 11
                            }
                        }
                    },
                    y: {
                        display: true,
                        grid: {
                            color: COLORS.grid,
                            drawBorder: false
                        },
                        ticks: {
                            color: COLORS.text,
                            font: {
                                size: 11
                            },
                            padding: 8
                        },
                        beginAtZero: true
                    }
                },
                plugins: {
                    ...DEFAULT_OPTIONS.plugins,
                    tooltip: {
                        ...DEFAULT_OPTIONS.plugins.tooltip,
                        callbacks: {
                            title: (items) => items[0]?.label || '',
                            label: (context) => `Count: ${context.parsed.y}`
                        }
                    }
                }
            }
        });
    };

    /**
     * Create a donut/pie chart.
     * @param {HTMLCanvasElement} canvas - Canvas element
     * @param {Object} options - Chart options
     * @param {string[]} options.labels - Segment labels
     * @param {number[]} options.values - Data values
     * @param {string[]} [options.colors] - Segment colors
     * @returns {Chart|null} Chart instance
     */
    Athena.charts.createDonut = function(canvas, options = {}) {
        if (!window.Chart) {
            console.warn('[AthenaCharts] Chart.js not loaded');
            return null;
        }

        const {
            labels = [],
            values = [],
            colors = [COLORS.success, COLORS.warning, COLORS.error, COLORS.gray]
        } = options;

        const ctx = canvas.getContext('2d');

        return new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: labels,
                datasets: [{
                    data: values,
                    backgroundColor: colors,
                    borderColor: '#1a1a1a',
                    borderWidth: 2,
                    hoverOffset: 4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: '70%',
                plugins: {
                    legend: {
                        display: true,
                        position: 'right',
                        labels: {
                            color: COLORS.text,
                            font: {
                                size: 12
                            },
                            padding: 16,
                            usePointStyle: true,
                            pointStyle: 'circle'
                        }
                    },
                    tooltip: DEFAULT_OPTIONS.plugins.tooltip
                }
            }
        });
    };

    /**
     * Create a multi-line chart.
     * @param {HTMLCanvasElement} canvas - Canvas element
     * @param {Object} options - Chart options
     * @param {string[]} options.labels - X-axis labels
     * @param {Array<{label: string, data: number[], color: string}>} options.datasets - Line datasets
     * @returns {Chart|null} Chart instance
     */
    Athena.charts.createMultiLine = function(canvas, options = {}) {
        if (!window.Chart) {
            console.warn('[AthenaCharts] Chart.js not loaded');
            return null;
        }

        const {
            labels = [],
            datasets = []
        } = options;

        const ctx = canvas.getContext('2d');

        const chartDatasets = datasets.map(ds => ({
            label: ds.label,
            data: ds.data,
            borderColor: ds.color || COLORS.primary,
            backgroundColor: 'transparent',
            borderWidth: 2,
            tension: 0.4,
            pointRadius: 0,
            pointHoverRadius: 4,
            pointHoverBackgroundColor: ds.color || COLORS.primary
        }));

        return new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: chartDatasets
            },
            options: {
                ...DEFAULT_OPTIONS,
                scales: {
                    x: {
                        display: true,
                        grid: {
                            color: COLORS.grid,
                            drawBorder: false
                        },
                        ticks: {
                            color: COLORS.text,
                            font: {
                                size: 11
                            }
                        }
                    },
                    y: {
                        display: true,
                        grid: {
                            color: COLORS.grid,
                            drawBorder: false
                        },
                        ticks: {
                            color: COLORS.text,
                            font: {
                                size: 11
                            }
                        },
                        beginAtZero: true
                    }
                },
                plugins: {
                    ...DEFAULT_OPTIONS.plugins,
                    legend: {
                        display: datasets.length > 1,
                        position: 'top',
                        align: 'end',
                        labels: {
                            color: COLORS.text,
                            font: {
                                size: 11
                            },
                            usePointStyle: true,
                            padding: 16
                        }
                    }
                }
            }
        });
    };

    /**
     * Update chart data without recreating.
     * @param {Chart} chart - Chart instance
     * @param {number[]} newData - New data points
     * @param {number} [datasetIndex=0] - Dataset index to update
     */
    Athena.charts.updateData = function(chart, newData, datasetIndex = 0) {
        if (!chart || !chart.data) return;

        chart.data.datasets[datasetIndex].data = newData;
        chart.data.labels = newData.map((_, i) => i);
        chart.update('none'); // Update without animation
    };

    /**
     * Destroy a chart instance.
     * @param {Chart} chart - Chart instance to destroy
     */
    Athena.charts.destroy = function(chart) {
        if (chart && typeof chart.destroy === 'function') {
            chart.destroy();
        }
    };

    // Export colors for external use
    Athena.charts.COLORS = COLORS;

})(window.Athena);
