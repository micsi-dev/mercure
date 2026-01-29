/**
 * Advanced DICOM Viewer with MPR, Windowing, Colormaps, and 4D Support
 * Uses canvas-based rendering with server-side MPR generation
 */

// Global state for the advanced viewer
let advancedViewerState = {
    taskId: null,
    selectedSeriesUid: null,  // Selected series UID for filtering
    volumeInfo: null,
    is4D: false,
    currentTimepoint: 1,
    isPlaying: false,
    playInterval: null,
    activeTool: 'crosshairs',
    windowWidth: 400,
    windowLevel: 40,
    sliceIndices: {
        axial: 0,
        sagittal: 0,
        coronal: 0
    },
    maxSlices: {
        axial: 0,
        sagittal: 0,
        coronal: 0
    },
    zoom: {
        axial: 1,
        sagittal: 1,
        coronal: 1
    },
    pan: {
        axial: { x: 0, y: 0 },
        sagittal: { x: 0, y: 0 },
        coronal: { x: 0, y: 0 }
    },
    isDragging: false,
    dragStart: { x: 0, y: 0 },
    activeViewport: null,
    imageCache: {},
    loadingImages: {},
    pendingRequests: {},  // Track request IDs to ignore stale responses
    maxConcurrentPreloads: 4, // Limit concurrent preload requests
    activePreloads: 0,
    crosshairsEnabled: true,
    windowingDebounceTimer: null,
    colormap: 'gray',
    showColorbar: false,
    interpolation: true,
    focusedViewport: 'axial',
    viewLayout: 'all', // 'all', 'axial', 'sagittal', 'coronal'
    // Scroll optimization state
    lastScrollTime: 0,
    scrollThrottleMs: 30,  // Minimum ms between scroll events
    pendingScrollRender: null,
    isScrolling: false,
    scrollEndTimer: null
};

// Windowing presets
const windowingPresets = {
    ct_brain: { width: 80, level: 40 },
    ct_bone: { width: 2000, level: 500 },
    ct_lung: { width: 1500, level: -600 },
    ct_abdomen: { width: 400, level: 40 },
    ct_liver: { width: 150, level: 30 },
    mr_t1: { width: 500, level: 250 },
    mr_t2: { width: 400, level: 200 },
    pet_suv: { width: 10, level: 5 }
};

// Colormap definitions for colorbar rendering
const colormapDefs = {
    gray: (t) => [t * 255, t * 255, t * 255],
    inverted: (t) => [255 - t * 255, 255 - t * 255, 255 - t * 255],
    hot: (t) => {
        if (t < 0.33) return [t * 3 * 255, 0, 0];
        if (t < 0.66) return [255, (t - 0.33) * 3 * 255, 0];
        return [255, 255, (t - 0.66) * 3 * 255];
    },
    cool: (t) => [t * 255, 255 - t * 255, 255],
    jet: (t) => {
        if (t < 0.125) return [0, 0, 128 + t * 8 * 127];
        if (t < 0.375) return [0, (t - 0.125) * 4 * 255, 255];
        if (t < 0.625) return [(t - 0.375) * 4 * 255, 255, 255 - (t - 0.375) * 4 * 255];
        if (t < 0.875) return [255, 255 - (t - 0.625) * 4 * 255, 0];
        return [255 - (t - 0.875) * 8 * 127, 0, 0];
    }
};

/**
 * Open the advanced DICOM viewer for a task
 * @param {string} taskId - The task ID
 * @param {string} [seriesUid] - Optional series UID to filter by
 */
async function openAdvancedDicomViewer(taskId, seriesUid = null) {
    advancedViewerState.taskId = taskId;
    advancedViewerState.selectedSeriesUid = seriesUid;
    advancedViewerState.imageCache = {};
    advancedViewerState.loadingImages = {};

    // Show modal and loading state
    $('#advanced_dicom_viewer_modal').addClass('is-active');
    $('#advanced_viewer_task_id').text(taskId.substring(0, 8) + '...');
    $('#advanced_viewer_loading').removeClass('is-hidden');
    updateLoadingStatus('Fetching volume information...', 5);

    try {
        // Fetch volume metadata (with series filter if specified)
        let volumeUrl = '/api/task-dicom-volume-info/' + taskId;
        if (seriesUid) {
            volumeUrl += '?series_uid=' + encodeURIComponent(seriesUid);
        }
        const response = await fetch(volumeUrl);
        if (!response.ok) {
            throw new Error('Failed to fetch volume info: ' + response.statusText);
        }

        const volumeInfo = await response.json();
        if (volumeInfo.error) {
            throw new Error(volumeInfo.error);
        }

        advancedViewerState.volumeInfo = volumeInfo;
        advancedViewerState.is4D = volumeInfo.is_4d;

        // Get the acquisition plane
        const acquisitionPlane = volumeInfo.orientation?.acquisition_plane || 'axial';
        console.log('Acquisition plane:', acquisitionPlane);

        // Set max slices based on acquisition plane
        // For the native plane, use slices count; for reconstructed planes, use rows/columns
        if (acquisitionPlane === 'axial') {
            advancedViewerState.maxSlices.axial = volumeInfo.dimensions.slices;
            advancedViewerState.maxSlices.sagittal = volumeInfo.dimensions.columns;
            advancedViewerState.maxSlices.coronal = volumeInfo.dimensions.rows;
        } else if (acquisitionPlane === 'coronal') {
            advancedViewerState.maxSlices.coronal = volumeInfo.dimensions.slices;
            advancedViewerState.maxSlices.axial = volumeInfo.dimensions.rows;
            advancedViewerState.maxSlices.sagittal = volumeInfo.dimensions.columns;
        } else if (acquisitionPlane === 'sagittal') {
            advancedViewerState.maxSlices.sagittal = volumeInfo.dimensions.slices;
            advancedViewerState.maxSlices.axial = volumeInfo.dimensions.rows;
            advancedViewerState.maxSlices.coronal = volumeInfo.dimensions.columns;
        }

        // Set initial slice indices to middle
        advancedViewerState.sliceIndices.axial = Math.floor(advancedViewerState.maxSlices.axial / 2);
        advancedViewerState.sliceIndices.sagittal = Math.floor(advancedViewerState.maxSlices.sagittal / 2);
        advancedViewerState.sliceIndices.coronal = Math.floor(advancedViewerState.maxSlices.coronal / 2);

        // Auto-apply percentile windowing if available, otherwise use default
        if (volumeInfo.windowing.percentile_min !== null && volumeInfo.windowing.percentile_max !== null) {
            const min = volumeInfo.windowing.percentile_min;
            const max = volumeInfo.windowing.percentile_max;
            advancedViewerState.windowWidth = max - min;
            advancedViewerState.windowLevel = (max + min) / 2;
        } else {
            advancedViewerState.windowWidth = volumeInfo.windowing.default_width;
            advancedViewerState.windowLevel = volumeInfo.windowing.default_center;
        }

        updateLoadingStatus('Initializing viewer...', 20);

        // Update volume info display
        updateVolumeInfoDisplay(volumeInfo);

        // Setup canvases
        setupCanvases();

        // Setup controls
        setupWindowingControls(volumeInfo);
        setupToolHandlers();
        setupDisplayControls();
        setupViewLayoutControls();
        setupKeyboardControls();

        // Setup 4D controls if applicable
        if (volumeInfo.is_4d) {
            setup4DControls(volumeInfo.dimensions.timepoints);
        } else {
            $('#controls_4d').hide();
        }

        updateLoadingStatus('Loading initial views...', 40);

        // Load all three views
        await Promise.all([
            loadViewportImage('axial'),
            loadViewportImage('sagittal'),
            loadViewportImage('coronal')
        ]);

        // Draw crosshairs (enabled by default)
        updateCrosshairs();

        // Preload adjacent slices for smoother scrolling
        preloadAdjacentSlices();

        // Hide loading overlay
        $('#advanced_viewer_loading').addClass('is-hidden');

        // Focus the modal for keyboard events
        $('#advanced_dicom_viewer_modal').focus();

    } catch (error) {
        console.error('Error opening advanced viewer:', error);
        alert('Failed to open advanced viewer: ' + error.message);
        closeAdvancedDicomViewer();
    }
}

/**
 * Update loading status display
 */
function updateLoadingStatus(message, progress) {
    $('#loading_status').text(message);
    $('#loading_progress').val(progress);
}

/**
 * Update volume info display panel
 */
function updateVolumeInfoDisplay(volumeInfo) {
    $('#info_modality').text(volumeInfo.modality);
    $('#info_dimensions').text(volumeInfo.dimensions.columns + ' x ' + volumeInfo.dimensions.rows);
    $('#info_slices').text(volumeInfo.dimensions.slices + (volumeInfo.is_4d ? ' x ' + volumeInfo.dimensions.timepoints + ' timepoints' : ''));
    $('#info_spacing').text(
        volumeInfo.spacing.pixel_spacing[0].toFixed(2) + ' x ' +
        volumeInfo.spacing.pixel_spacing[1].toFixed(2) + ' x ' +
        volumeInfo.spacing.slice_thickness.toFixed(2) + ' mm'
    );
}

/**
 * Setup canvas elements for each viewport
 */
function setupCanvases() {
    const viewports = ['axial', 'sagittal', 'coronal'];

    viewports.forEach(orientation => {
        const container = document.getElementById('viewport_' + orientation);
        container.innerHTML = '';

        const canvas = document.createElement('canvas');
        canvas.id = 'canvas_' + orientation;
        canvas.className = 'viewport-canvas';
        canvas.style.width = '100%';
        canvas.style.height = '100%';
        container.appendChild(canvas);

        // Setup event listeners for this canvas
        setupCanvasEventListeners(canvas, orientation);
    });
}

/**
 * Setup mouse event listeners for canvas interactions
 */
function setupCanvasEventListeners(canvas, orientation) {
    // Track focus for keyboard navigation
    canvas.addEventListener('click', () => {
        advancedViewerState.focusedViewport = orientation;
        // Visual feedback for focused viewport
        $('.viewport-container').removeClass('is-focused');
        $(`#viewport_${orientation}_container`).addClass('is-focused');
    });

    // Mouse wheel for scrolling (always scrolls slices, except zoom tool uses wheel to zoom)
    canvas.addEventListener('wheel', (e) => {
        e.preventDefault();
        const delta = e.deltaY > 0 ? 1 : -1;

        if (advancedViewerState.activeTool === 'zoom') {
            const zoomDelta = delta > 0 ? 0.9 : 1.1;
            advancedViewerState.zoom[orientation] *= zoomDelta;
            advancedViewerState.zoom[orientation] = Math.max(0.1, Math.min(10, advancedViewerState.zoom[orientation]));
            renderViewport(orientation);
        } else {
            scrollSlice(orientation, delta);
        }
    });

    // Mouse down
    canvas.addEventListener('mousedown', (e) => {
        advancedViewerState.isDragging = true;
        advancedViewerState.dragStart = { x: e.clientX, y: e.clientY };
        advancedViewerState.activeViewport = orientation;
        advancedViewerState.focusedViewport = orientation;

        if (advancedViewerState.activeTool === 'crosshairs') {
            handleCrosshairsClick(e, canvas, orientation);
        }
    });

    // Mouse move - track position and handle dragging
    canvas.addEventListener('mousemove', (e) => {
        // Always update pixel info on mouse move
        updatePixelInfo(e, canvas, orientation);

        // Handle tool dragging
        if (advancedViewerState.isDragging && advancedViewerState.activeViewport === orientation) {
            const dx = e.clientX - advancedViewerState.dragStart.x;
            const dy = e.clientY - advancedViewerState.dragStart.y;

            switch (advancedViewerState.activeTool) {
                case 'pan':
                    advancedViewerState.pan[orientation].x += dx;
                    advancedViewerState.pan[orientation].y += dy;
                    renderViewport(orientation);
                    break;
                case 'zoom':
                    const zoomDelta = 1 + dy * 0.005;
                    advancedViewerState.zoom[orientation] *= zoomDelta;
                    advancedViewerState.zoom[orientation] = Math.max(0.1, Math.min(10, advancedViewerState.zoom[orientation]));
                    renderViewport(orientation);
                    break;
                case 'window':
                    advancedViewerState.windowWidth += dx * 2;
                    advancedViewerState.windowLevel -= dy * 2;
                    advancedViewerState.windowWidth = Math.max(1, advancedViewerState.windowWidth);
                    updateWindowingDisplay();
                    $('#windowing_preset').val('custom');
                    debouncedWindowingUpdate();
                    break;
                case 'crosshairs':
                    handleCrosshairsClick(e, canvas, orientation);
                    break;
            }

            advancedViewerState.dragStart = { x: e.clientX, y: e.clientY };
        }
    });

    // Mouse up
    canvas.addEventListener('mouseup', () => {
        advancedViewerState.isDragging = false;
        advancedViewerState.activeViewport = null;
    });

    canvas.addEventListener('mouseleave', () => {
        advancedViewerState.isDragging = false;
        clearPixelInfo();
    });
}

/**
 * Update pixel info display based on cursor position
 */
function updatePixelInfo(e, canvas, orientation) {
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    const zoom = advancedViewerState.zoom[orientation];
    const pan = advancedViewerState.pan[orientation];

    // Get cached image info
    const sliceIndex = advancedViewerState.sliceIndices[orientation];
    const ww = Math.round(advancedViewerState.windowWidth);
    const wc = Math.round(advancedViewerState.windowLevel);
    const cmap = advancedViewerState.colormap;
    const cacheKey = `${orientation}_${sliceIndex}_${ww}_${wc}_${cmap}`;
    const img = advancedViewerState.imageCache[cacheKey];

    if (!img) {
        clearPixelInfo();
        return;
    }

    // Calculate image bounds on canvas
    const imgAspect = img.width / img.height;
    const canvasAspect = canvas.width / canvas.height;
    let drawWidth, drawHeight;
    if (imgAspect > canvasAspect) {
        drawWidth = canvas.width * zoom;
        drawHeight = (canvas.width / imgAspect) * zoom;
    } else {
        drawHeight = canvas.height * zoom;
        drawWidth = (canvas.height * imgAspect) * zoom;
    }
    const imgX = (canvas.width - drawWidth) / 2 + pan.x;
    const imgY = (canvas.height - drawHeight) / 2 + pan.y;

    // Calculate normalized coordinates within image
    const normX = (x - imgX) / drawWidth;
    const normY = (y - imgY) / drawHeight;

    // Check if cursor is within image bounds
    if (normX < 0 || normX > 1 || normY < 0 || normY > 1) {
        clearPixelInfo();
        return;
    }

    // Calculate pixel coordinates
    const pixelX = Math.floor(normX * img.width);
    const pixelY = Math.floor(normY * img.height);

    // Get pixel value from canvas
    const ctx = canvas.getContext('2d');
    const pixelData = ctx.getImageData(Math.floor(x), Math.floor(y), 1, 1).data;
    const pixelValue = pixelData[0]; // Use red channel for grayscale

    // Calculate approximate original value (reverse windowing approximation)
    const minVal = wc - ww / 2;
    const maxVal = wc + ww / 2;
    const approxOriginal = Math.round(minVal + (pixelValue / 255) * (maxVal - minVal));

    // Update display
    $('#pixel_info_coords').text(`(${pixelX}, ${pixelY})`);
    $('#pixel_info_value').text(approxOriginal);
    $('#pixel_info_slice').text(sliceIndex + 1);
    $('#pixel_info_view').text(orientation.charAt(0).toUpperCase() + orientation.slice(1));
    $('#pixel_info_container').show();
    $('#pixel_info_placeholder').hide();
}

/**
 * Clear pixel info display
 */
function clearPixelInfo() {
    $('#pixel_info_container').hide();
    $('#pixel_info_placeholder').show();
}

/**
 * Scroll through slices with throttling for smooth performance
 */
function scrollSlice(orientation, delta) {
    const now = performance.now();

    // Calculate new index immediately
    let newIndex = advancedViewerState.sliceIndices[orientation] + delta;
    newIndex = Math.max(0, Math.min(newIndex, advancedViewerState.maxSlices[orientation] - 1));

    if (newIndex === advancedViewerState.sliceIndices[orientation]) {
        return; // No change
    }

    // Update slice index immediately
    advancedViewerState.sliceIndices[orientation] = newIndex;

    // Throttle the actual render
    if (now - advancedViewerState.lastScrollTime < advancedViewerState.scrollThrottleMs) {
        // Schedule a render if we're throttling
        if (!advancedViewerState.pendingScrollRender) {
            advancedViewerState.pendingScrollRender = requestAnimationFrame(() => {
                advancedViewerState.pendingScrollRender = null;
                performScrollRender(orientation);
            });
        }
        return;
    }

    advancedViewerState.lastScrollTime = now;
    performScrollRender(orientation);
}

/**
 * Perform the actual scroll render
 */
function performScrollRender(orientation) {
    // Mark as scrolling and cancel any pending preloads
    if (!advancedViewerState.isScrolling) {
        advancedViewerState.isScrolling = true;
        cancelAllPreloads();
    }

    // Clear any pending scroll end timer
    if (advancedViewerState.scrollEndTimer) {
        clearTimeout(advancedViewerState.scrollEndTimer);
    }

    // Load the viewport image (will use cache if available)
    loadViewportImage(orientation);

    // Update slice info immediately
    updateSliceInfo(orientation);

    // Delay crosshairs update slightly during fast scroll
    if (advancedViewerState.crosshairsEnabled) {
        updateCrosshairs();
    }

    // Schedule scroll end handling
    advancedViewerState.scrollEndTimer = setTimeout(() => {
        advancedViewerState.isScrolling = false;
        // Preload more aggressively when scroll ends
        preloadAdjacentSlices(orientation);
    }, 150);
}

/**
 * Setup keyboard controls
 */
function setupKeyboardControls() {
    $(document).off('keydown.advancedViewer').on('keydown.advancedViewer', function(e) {
        if (!$('#advanced_dicom_viewer_modal').hasClass('is-active')) return;

        const orientation = advancedViewerState.focusedViewport;

        switch (e.keyCode) {
            case 37: // Left arrow
                e.preventDefault();
                scrollSlice(orientation, -1);
                break;
            case 39: // Right arrow
                e.preventDefault();
                scrollSlice(orientation, 1);
                break;
            case 38: // Up arrow
                e.preventDefault();
                scrollSlice(orientation, -1);
                break;
            case 40: // Down arrow
                e.preventDefault();
                scrollSlice(orientation, 1);
                break;
            case 49: // 1 - focus axial
                advancedViewerState.focusedViewport = 'axial';
                $('.viewport-container').removeClass('is-focused');
                $('#viewport_axial_container').addClass('is-focused');
                break;
            case 50: // 2 - focus sagittal
                advancedViewerState.focusedViewport = 'sagittal';
                $('.viewport-container').removeClass('is-focused');
                $('#viewport_sagittal_container').addClass('is-focused');
                break;
            case 51: // 3 - focus coronal
                advancedViewerState.focusedViewport = 'coronal';
                $('.viewport-container').removeClass('is-focused');
                $('#viewport_coronal_container').addClass('is-focused');
                break;
            case 82: // R - reset
                resetViewports();
                break;
            case 27: // Escape
                closeAdvancedDicomViewer();
                break;
        }
    });
}

/**
 * Setup display controls (colormap, colorbar, interpolation)
 */
function setupDisplayControls() {
    // Colormap selector
    $('#colormap_select').off('change').on('change', function() {
        advancedViewerState.colormap = $(this).val();
        advancedViewerState.imageCache = {}; // Clear cache for new colormap
        loadAllViewports();
    });

    // Colorbar toggle
    $('#colorbar_toggle').off('change').on('change', function() {
        advancedViewerState.showColorbar = $(this).is(':checked');
        ['axial', 'sagittal', 'coronal'].forEach(orient => renderViewport(orient));
    });

    // Interpolation toggle
    $('#interpolation_toggle').off('change').on('change', function() {
        advancedViewerState.interpolation = $(this).is(':checked');
        ['axial', 'sagittal', 'coronal'].forEach(orient => renderViewport(orient));
    });
}

/**
 * Setup view layout controls
 */
function setupViewLayoutControls() {
    $('.view-layout-buttons .button').off('click').on('click', function() {
        const layout = $(this).data('layout');
        setViewLayout(layout);
    });
}

/**
 * Set view layout (all views or single view)
 */
function setViewLayout(layout) {
    advancedViewerState.viewLayout = layout;

    // Update button states
    $('.view-layout-buttons .button').removeClass('is-active');
    $(`.view-layout-buttons .button[data-layout="${layout}"]`).addClass('is-active');

    const viewerGrid = $('.viewer-grid');
    const viewportContainers = {
        axial: $('#viewport_axial_container'),
        sagittal: $('#viewport_sagittal_container'),
        coronal: $('#viewport_coronal_container')
    };

    if (layout === 'all') {
        // Show all viewports in grid
        viewerGrid.removeClass('single-view');
        Object.values(viewportContainers).forEach(container => {
            container.removeClass('is-visible').show();
        });
    } else {
        // Show single viewport
        viewerGrid.addClass('single-view');
        Object.entries(viewportContainers).forEach(([orient, container]) => {
            if (orient === layout) {
                container.addClass('is-visible').show();
            } else {
                container.removeClass('is-visible').hide();
            }
        });

        // Focus the visible viewport
        advancedViewerState.focusedViewport = layout;
    }

    // Re-render to fit new container sizes
    setTimeout(() => {
        if (layout === 'all') {
            ['axial', 'sagittal', 'coronal'].forEach(orient => renderViewport(orient));
        } else {
            renderViewport(layout);
        }
    }, 50);
}

/**
 * Auto-window using percentile values
 */
function autoWindow() {
    const volumeInfo = advancedViewerState.volumeInfo;
    if (volumeInfo.windowing.percentile_min !== null && volumeInfo.windowing.percentile_max !== null) {
        const min = volumeInfo.windowing.percentile_min;
        const max = volumeInfo.windowing.percentile_max;
        advancedViewerState.windowWidth = max - min;
        advancedViewerState.windowLevel = (max + min) / 2;
        updateWindowingDisplay();
        $('#windowing_preset').val('custom');
        advancedViewerState.imageCache = {};
        loadAllViewports();
    }
}

/**
 * Get axis inversion flags based on acquisition plane.
 * These flags determine whether click/crosshair positions need to be inverted
 * for each axis mapping between views.
 *
 * The inversions account for:
 * 1. Standard radiological display conventions (S at top, A at front, etc.)
 * 2. How slice indices map to patient coordinates for each acquisition type
 *
 * For axial acquisition: slices ordered I→S (slice 0 = inferior)
 * For coronal acquisition: rows ordered S→I (row 0 = superior), slices ordered A→P
 * For sagittal acquisition: rows ordered S→I (row 0 = superior), slices ordered R→L
 */
function getAxisInversions(acquisitionPlane) {
    // Default inversions for axial acquisition (the standard case)
    const inversions = {
        // Axial view clicks
        axialClickToSagittal: false,   // normX → sagittal slice
        axialClickToCoronal: false,    // normY → coronal slice
        // Sagittal view clicks
        sagittalClickToCoronal: true,  // normX → coronal slice (A at left, but slices A→P)
        sagittalClickToAxial: true,    // normY → axial slice (S at top, slices I→S)
        // Coronal view clicks
        coronalClickToSagittal: false, // normX → sagittal slice
        coronalClickToAxial: true,     // normY → axial slice (S at top, slices I→S)
        // For drawing crosshairs (inverse mappings)
        axialToSagittalCross: false,
        axialToCoronalCross: false,
        sagittalToCoronalCross: true,
        sagittalToAxialCross: true,
        coronalToSagittalCross: false,
        coronalToAxialCross: true
    };

    if (acquisitionPlane === 'coronal') {
        // Coronal acquisition: axial slices come from rows, ordered S→I
        // So slice 0 = superior, slice max = inferior
        // When clicking at top (normY=0, superior), we want slice 0
        inversions.sagittalClickToAxial = false;
        inversions.coronalClickToAxial = false;
        inversions.sagittalToAxialCross = false;
        inversions.coronalToAxialCross = false;
    } else if (acquisitionPlane === 'sagittal') {
        // Sagittal acquisition: similar to coronal for axial reconstruction
        inversions.sagittalClickToAxial = false;
        inversions.coronalClickToAxial = false;
        inversions.sagittalToAxialCross = false;
        inversions.coronalToAxialCross = false;
    }

    return inversions;
}

/**
 * Handle crosshairs click - set slice positions in other viewports
 * This needs to account for the acquisition plane since the image axes
 * may be different from standard axial acquisition.
 */
function handleCrosshairsClick(e, canvas, orientation) {
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;

    const zoom = advancedViewerState.zoom[orientation];
    const pan = advancedViewerState.pan[orientation];

    const sliceIndex = advancedViewerState.sliceIndices[orientation];
    const ww = Math.round(advancedViewerState.windowWidth);
    const wc = Math.round(advancedViewerState.windowLevel);
    const cmap = advancedViewerState.colormap;
    const cacheKey = `${orientation}_${sliceIndex}_${ww}_${wc}_${cmap}`;
    const img = advancedViewerState.imageCache[cacheKey];

    if (!img) return;

    const imgAspect = img.width / img.height;
    const canvasAspect = canvas.width / canvas.height;
    let drawWidth, drawHeight;
    if (imgAspect > canvasAspect) {
        drawWidth = canvas.width * zoom;
        drawHeight = (canvas.width / imgAspect) * zoom;
    } else {
        drawHeight = canvas.height * zoom;
        drawWidth = (canvas.height * imgAspect) * zoom;
    }
    const imgX = (canvas.width - drawWidth) / 2 + pan.x;
    const imgY = (canvas.height - drawHeight) / 2 + pan.y;

    const normX = (x - imgX) / drawWidth;
    const normY = (y - imgY) / drawHeight;

    if (normX < 0 || normX > 1 || normY < 0 || normY > 1) return;

    // Use maxSlices which is already corrected for the acquisition plane
    const maxSlices = advancedViewerState.maxSlices;
    const acquisitionPlane = advancedViewerState.volumeInfo?.orientation?.acquisition_plane || 'axial';

    // Get the axis inversion flags based on acquisition plane
    const axisInversions = getAxisInversions(acquisitionPlane);

    if (orientation === 'axial') {
        // Clicking on axial: X horizontal, Y vertical
        // normX -> sagittal slice (X position)
        // normY -> coronal slice (Y position)
        const sagNorm = axisInversions.axialClickToSagittal ? (1 - normX) : normX;
        const corNorm = axisInversions.axialClickToCoronal ? (1 - normY) : normY;
        advancedViewerState.sliceIndices.sagittal = Math.floor(sagNorm * maxSlices.sagittal);
        advancedViewerState.sliceIndices.coronal = Math.floor(corNorm * maxSlices.coronal);
        loadViewportImage('sagittal');
        loadViewportImage('coronal');
    } else if (orientation === 'sagittal') {
        // Clicking on sagittal: Y horizontal (A-P), Z vertical (S-I)
        // normX -> coronal slice (Y position)
        // normY -> axial slice (Z position)
        const corNorm = axisInversions.sagittalClickToCoronal ? (1 - normX) : normX;
        const axNorm = axisInversions.sagittalClickToAxial ? (1 - normY) : normY;
        advancedViewerState.sliceIndices.coronal = Math.floor(corNorm * maxSlices.coronal);
        advancedViewerState.sliceIndices.axial = Math.floor(axNorm * maxSlices.axial);
        loadViewportImage('coronal');
        loadViewportImage('axial');
    } else if (orientation === 'coronal') {
        // Clicking on coronal: X horizontal (R-L), Z vertical (S-I)
        // normX -> sagittal slice (X position)
        // normY -> axial slice (Z position)
        const sagNorm = axisInversions.coronalClickToSagittal ? (1 - normX) : normX;
        const axNorm = axisInversions.coronalClickToAxial ? (1 - normY) : normY;
        advancedViewerState.sliceIndices.sagittal = Math.floor(sagNorm * maxSlices.sagittal);
        advancedViewerState.sliceIndices.axial = Math.floor(axNorm * maxSlices.axial);
        loadViewportImage('sagittal');
        loadViewportImage('axial');
    }

    updateCrosshairs();
}

/**
 * Update crosshairs display on all viewports
 */
function updateCrosshairs() {
    if (!advancedViewerState.crosshairsEnabled) return;
    ['axial', 'sagittal', 'coronal'].forEach(orientation => {
        renderViewport(orientation);
    });
}

/**
 * Draw crosshairs on a viewport
 */
function drawCrosshairs(ctx, canvas, orientation) {
    if (!advancedViewerState.crosshairsEnabled) return;

    const maxSlices = advancedViewerState.maxSlices;
    const zoom = advancedViewerState.zoom[orientation];
    const pan = advancedViewerState.pan[orientation];

    const sliceIndex = advancedViewerState.sliceIndices[orientation];
    const ww = Math.round(advancedViewerState.windowWidth);
    const wc = Math.round(advancedViewerState.windowLevel);
    const cmap = advancedViewerState.colormap;
    const cacheKey = `${orientation}_${sliceIndex}_${ww}_${wc}_${cmap}`;
    const img = advancedViewerState.imageCache[cacheKey];

    if (!img) return;

    const imgAspect = img.width / img.height;
    const canvasAspect = canvas.width / canvas.height;
    let drawWidth, drawHeight;
    if (imgAspect > canvasAspect) {
        drawWidth = canvas.width * zoom;
        drawHeight = (canvas.width / imgAspect) * zoom;
    } else {
        drawHeight = canvas.height * zoom;
        drawWidth = (canvas.height * imgAspect) * zoom;
    }
    const imgX = (canvas.width - drawWidth) / 2 + pan.x;
    const imgY = (canvas.height - drawHeight) / 2 + pan.y;

    let crossX, crossY;

    // Use maxSlices which is corrected for the acquisition plane
    const acquisitionPlane = advancedViewerState.volumeInfo?.orientation?.acquisition_plane || 'axial';
    const axisInversions = getAxisInversions(acquisitionPlane);

    if (orientation === 'axial') {
        // Axial: X horizontal (sagittal position), Y vertical (coronal position)
        const sagNorm = advancedViewerState.sliceIndices.sagittal / maxSlices.sagittal;
        const corNorm = advancedViewerState.sliceIndices.coronal / maxSlices.coronal;
        crossX = imgX + (axisInversions.axialToSagittalCross ? (1 - sagNorm) : sagNorm) * drawWidth;
        crossY = imgY + (axisInversions.axialToCoronalCross ? (1 - corNorm) : corNorm) * drawHeight;
    } else if (orientation === 'sagittal') {
        // Sagittal: Y horizontal (coronal position), Z vertical (axial position)
        const corNorm = advancedViewerState.sliceIndices.coronal / maxSlices.coronal;
        const axNorm = advancedViewerState.sliceIndices.axial / maxSlices.axial;
        crossX = imgX + (axisInversions.sagittalToCoronalCross ? (1 - corNorm) : corNorm) * drawWidth;
        crossY = imgY + (axisInversions.sagittalToAxialCross ? (1 - axNorm) : axNorm) * drawHeight;
    } else if (orientation === 'coronal') {
        // Coronal: X horizontal (sagittal position), Z vertical (axial position)
        const sagNorm = advancedViewerState.sliceIndices.sagittal / maxSlices.sagittal;
        const axNorm = advancedViewerState.sliceIndices.axial / maxSlices.axial;
        crossX = imgX + (axisInversions.coronalToSagittalCross ? (1 - sagNorm) : sagNorm) * drawWidth;
        crossY = imgY + (axisInversions.coronalToAxialCross ? (1 - axNorm) : axNorm) * drawHeight;
    }

    ctx.strokeStyle = '#00ff00';
    ctx.lineWidth = 1;
    ctx.setLineDash([5, 5]);

    ctx.beginPath();
    ctx.moveTo(crossX, imgY);
    ctx.lineTo(crossX, imgY + drawHeight);
    ctx.stroke();

    ctx.beginPath();
    ctx.moveTo(imgX, crossY);
    ctx.lineTo(imgX + drawWidth, crossY);
    ctx.stroke();

    ctx.setLineDash([]);
}

/**
 * Draw colorbar on viewport
 */
function drawColorbar(ctx, canvas) {
    if (!advancedViewerState.showColorbar) return;

    const barWidth = 20;
    const barHeight = canvas.height - 60;
    const barX = canvas.width - barWidth - 10;
    const barY = 30;

    const cmap = advancedViewerState.colormap;
    const cmapFunc = colormapDefs[cmap] || colormapDefs.gray;

    // Draw colorbar gradient
    for (let i = 0; i < barHeight; i++) {
        const t = 1 - (i / barHeight);
        const [r, g, b] = cmapFunc(t);
        ctx.fillStyle = `rgb(${Math.round(r)},${Math.round(g)},${Math.round(b)})`;
        ctx.fillRect(barX, barY + i, barWidth, 1);
    }

    // Draw border
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 1;
    ctx.strokeRect(barX, barY, barWidth, barHeight);

    // Draw labels
    const ww = advancedViewerState.windowWidth;
    const wc = advancedViewerState.windowLevel;
    const minVal = Math.round(wc - ww / 2);
    const maxVal = Math.round(wc + ww / 2);

    ctx.fillStyle = '#fff';
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(maxVal.toString(), barX - 3, barY + 10);
    ctx.fillText(minVal.toString(), barX - 3, barY + barHeight);
}

/**
 * Preload adjacent slices for smoother scrolling
 * Uses a wider range and prioritizes slices closer to current position
 */
function preloadAdjacentSlices(orientation = null) {
    const orientations = orientation ? [orientation] : ['axial', 'sagittal', 'coronal'];
    const ww = Math.round(advancedViewerState.windowWidth);
    const wc = Math.round(advancedViewerState.windowLevel);
    const cmap = advancedViewerState.colormap;

    // Preload range: more slices ahead for smoother scrolling
    const preloadRange = advancedViewerState.isScrolling ? 3 : 8;

    orientations.forEach(orient => {
        const currentSlice = advancedViewerState.sliceIndices[orient];
        const maxSlice = advancedViewerState.maxSlices[orient];

        // Prioritize: load closest slices first (interleaved pattern)
        const deltas = [];
        for (let i = 1; i <= preloadRange; i++) {
            deltas.push(i);
            deltas.push(-i);
        }

        deltas.forEach(delta => {
            const sliceIndex = currentSlice + delta;
            if (sliceIndex >= 0 && sliceIndex < maxSlice) {
                preloadImage(orient, sliceIndex, ww, wc, cmap);
            }
        });
    });
}

/**
 * Preload a single image into cache
 */
function preloadImage(orientation, sliceIndex, ww, wc, cmap) {
    const cacheKey = `${orientation}_${sliceIndex}_${ww}_${wc}_${cmap}`;

    if (advancedViewerState.imageCache[cacheKey] || advancedViewerState.loadingImages[cacheKey]) {
        return;
    }

    // Limit concurrent preloads
    if (advancedViewerState.activePreloads >= advancedViewerState.maxConcurrentPreloads) {
        return;
    }

    // Guard against missing volume info
    if (!advancedViewerState.volumeInfo) {
        return;
    }

    advancedViewerState.loadingImages[cacheKey] = true;
    advancedViewerState.activePreloads++;

    const taskId = advancedViewerState.taskId;
    const seriesUid = advancedViewerState.selectedSeriesUid;
    const acquisitionPlane = advancedViewerState.volumeInfo?.orientation?.acquisition_plane || 'axial';
    let url;

    // Only use native files if the requested orientation matches the acquisition plane
    if (orientation === acquisitionPlane) {
        const files = advancedViewerState.volumeInfo.files;
        if (sliceIndex >= 0 && sliceIndex < files.length) {
            const filename = files[sliceIndex].filename;
            url = `/api/task-dicom-image/${taskId}/${encodeURIComponent(filename)}?ww=${ww}&wc=${wc}&cmap=${cmap}`;
        } else {
            advancedViewerState.activePreloads--;
            delete advancedViewerState.loadingImages[cacheKey];
            return;
        }
    } else {
        url = `/api/task-dicom-mpr/${taskId}?orientation=${orientation}&slice=${sliceIndex}&ww=${ww}&wc=${wc}&cmap=${cmap}`;
        if (seriesUid) {
            url += '&series_uid=' + encodeURIComponent(seriesUid);
        }
    }

    const img = new Image();
    img.onload = () => {
        advancedViewerState.imageCache[cacheKey] = img;
        advancedViewerState.activePreloads--;
        delete advancedViewerState.loadingImages[cacheKey];
    };
    img.onerror = () => {
        advancedViewerState.activePreloads--;
        delete advancedViewerState.loadingImages[cacheKey];
    };
    img.src = url;
}

/**
 * Cancel/reset preload tracking when scrolling starts
 * Image() requests can't be cancelled, but we reset the counter
 * so new preloads can start when scrolling ends
 */
function cancelAllPreloads() {
    // Reset preload tracking - existing Image loads will complete
    // but won't block new ones after scroll ends
    advancedViewerState.loadingImages = {};
    advancedViewerState.activePreloads = 0;
}

/**
 * Debounced windowing update for smooth dragging
 */
function debouncedWindowingUpdate() {
    if (advancedViewerState.windowingDebounceTimer) {
        clearTimeout(advancedViewerState.windowingDebounceTimer);
    }
    advancedViewerState.windowingDebounceTimer = setTimeout(() => {
        loadAllViewports();
    }, 100);
}

/**
 * Update windowing display values
 */
function updateWindowingDisplay() {
    const ww = Math.round(advancedViewerState.windowWidth);
    const wc = Math.round(advancedViewerState.windowLevel);
    const min = Math.round(wc - ww / 2);
    const max = Math.round(wc + ww / 2);

    $('#window_width_value').text(ww);
    $('#window_level_value').text(wc);
    $('#window_min_input').val(min);
    $('#window_max_input').val(max);
}

/**
 * Load image for a specific viewport
 */
async function loadViewportImage(orientation) {
    const taskId = advancedViewerState.taskId;
    const seriesUid = advancedViewerState.selectedSeriesUid;
    const sliceIndex = advancedViewerState.sliceIndices[orientation];
    const ww = Math.round(advancedViewerState.windowWidth);
    const wc = Math.round(advancedViewerState.windowLevel);
    const cmap = advancedViewerState.colormap;

    const cacheKey = `${orientation}_${sliceIndex}_${ww}_${wc}_${cmap}`;

    if (advancedViewerState.imageCache[cacheKey]) {
        renderViewportWithImage(orientation, advancedViewerState.imageCache[cacheKey]);
        updateSliceInfo(orientation);
        return;
    }

    // Track requests by viewport to detect stale responses
    const requestKey = `${orientation}_current`;

    // Get the acquisition plane from volume info
    const acquisitionPlane = advancedViewerState.volumeInfo?.orientation?.acquisition_plane || 'axial';

    // Guard against missing volume info
    if (!advancedViewerState.volumeInfo) {
        console.error('Volume info not loaded');
        return;
    }

    let url;
    // Only use native files if the requested orientation matches the acquisition plane
    if (orientation === acquisitionPlane) {
        const files = advancedViewerState.volumeInfo.files;
        if (sliceIndex >= 0 && sliceIndex < files.length) {
            const filename = files[sliceIndex].filename;
            url = `/api/task-dicom-image/${taskId}/${encodeURIComponent(filename)}?ww=${ww}&wc=${wc}&cmap=${cmap}`;
        } else {
            return;
        }
    } else {
        // Use MPR endpoint for reconstructed views
        url = `/api/task-dicom-mpr/${taskId}?orientation=${orientation}&slice=${sliceIndex}&ww=${ww}&wc=${wc}&cmap=${cmap}`;
        if (seriesUid) {
            url += '&series_uid=' + encodeURIComponent(seriesUid);
        }
    }

    // Track this request so we can ignore stale responses
    const requestId = Date.now() + Math.random();
    advancedViewerState.pendingRequests[requestKey] = requestId;

    try {
        const img = new Image();
        await new Promise((resolve, reject) => {
            img.onload = () => {
                // Only update if this is still the current request
                if (advancedViewerState.pendingRequests[requestKey] === requestId) {
                    advancedViewerState.imageCache[cacheKey] = img;
                    // Only render if slice hasn't changed
                    if (advancedViewerState.sliceIndices[orientation] === sliceIndex) {
                        renderViewportWithImage(orientation, img);
                        updateSliceInfo(orientation);
                    }
                }
                resolve();
            };
            img.onerror = () => {
                console.error('Failed to load image for', orientation, url);
                reject(new Error('Image load failed'));
            };
            img.src = url;
        });
    } catch (error) {
        // Only log if this wasn't superseded by a newer request
        if (advancedViewerState.pendingRequests[requestKey] === requestId) {
            console.error('Error loading viewport image:', error);
        }
    } finally {
        // Clean up if we're still the current request
        if (advancedViewerState.pendingRequests[requestKey] === requestId) {
            delete advancedViewerState.pendingRequests[requestKey];
        }
    }
}

/**
 * Render viewport with loaded image
 */
function renderViewportWithImage(orientation, img) {
    const canvas = document.getElementById('canvas_' + orientation);
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    const container = canvas.parentElement;

    canvas.width = container.clientWidth;
    canvas.height = container.clientHeight;

    // Set interpolation mode
    ctx.imageSmoothingEnabled = advancedViewerState.interpolation;
    ctx.imageSmoothingQuality = 'high';

    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    const zoom = advancedViewerState.zoom[orientation];
    const pan = advancedViewerState.pan[orientation];

    const imgAspect = img.width / img.height;
    const canvasAspect = canvas.width / canvas.height;

    let drawWidth, drawHeight;
    if (imgAspect > canvasAspect) {
        drawWidth = canvas.width * zoom;
        drawHeight = (canvas.width / imgAspect) * zoom;
    } else {
        drawHeight = canvas.height * zoom;
        drawWidth = (canvas.height * imgAspect) * zoom;
    }

    const x = (canvas.width - drawWidth) / 2 + pan.x;
    const y = (canvas.height - drawHeight) / 2 + pan.y;

    ctx.drawImage(img, x, y, drawWidth, drawHeight);

    drawCrosshairs(ctx, canvas, orientation);
    drawColorbar(ctx, canvas);
}

/**
 * Render viewport (re-render from cache)
 */
function renderViewport(orientation) {
    const sliceIndex = advancedViewerState.sliceIndices[orientation];
    const ww = Math.round(advancedViewerState.windowWidth);
    const wc = Math.round(advancedViewerState.windowLevel);
    const cmap = advancedViewerState.colormap;
    const cacheKey = `${orientation}_${sliceIndex}_${ww}_${wc}_${cmap}`;

    if (advancedViewerState.imageCache[cacheKey]) {
        renderViewportWithImage(orientation, advancedViewerState.imageCache[cacheKey]);
    }
}

/**
 * Load all viewports
 */
function loadAllViewports() {
    loadViewportImage('axial');
    loadViewportImage('sagittal');
    loadViewportImage('coronal');
}

/**
 * Update slice info display
 */
function updateSliceInfo(orientation) {
    const current = advancedViewerState.sliceIndices[orientation] + 1;
    const max = advancedViewerState.maxSlices[orientation];
    $(`#${orientation}_slice_info`).text(`Slice: ${current}/${max}`);
}

/**
 * Setup windowing controls
 */
function setupWindowingControls(volumeInfo) {
    // Window values are already set in openAdvancedDicomViewer (using percentiles if available)
    // Just update the display and setup event handlers
    updateWindowingDisplay();

    // Set preset dropdown to custom since we're using auto-windowing
    $('#windowing_preset').val('custom');

    $('#windowing_preset').off('change').on('change', function() {
        const preset = $(this).val();
        if (preset !== 'custom' && windowingPresets[preset]) {
            const { width, level } = windowingPresets[preset];
            advancedViewerState.windowWidth = width;
            advancedViewerState.windowLevel = level;
            updateWindowingDisplay();
            advancedViewerState.imageCache = {};
            loadAllViewports();
        }
    });

    $('#window_min_input').off('change').on('change', function() {
        const min = parseInt($(this).val()) || 0;
        const max = parseInt($('#window_max_input').val()) || 0;
        advancedViewerState.windowWidth = max - min;
        advancedViewerState.windowLevel = (max + min) / 2;
        updateWindowingDisplay();
        $('#windowing_preset').val('custom');
        advancedViewerState.imageCache = {};
        loadAllViewports();
    });

    $('#window_max_input').off('change').on('change', function() {
        const min = parseInt($('#window_min_input').val()) || 0;
        const max = parseInt($(this).val()) || 0;
        advancedViewerState.windowWidth = max - min;
        advancedViewerState.windowLevel = (max + min) / 2;
        updateWindowingDisplay();
        $('#windowing_preset').val('custom');
        advancedViewerState.imageCache = {};
        loadAllViewports();
    });

    // Modality-based preset selection removed - using auto-windowing (percentile-based) by default
}

/**
 * Setup tool button handlers
 */
function setupToolHandlers() {
    $('.tool-btn').off('click').on('click', function() {
        const tool = $(this).data('tool');
        if (!tool) return;

        $('.tool-btn').removeClass('is-active');
        $(this).addClass('is-active');
        advancedViewerState.activeTool = tool;

        if (tool === 'crosshairs') {
            advancedViewerState.crosshairsEnabled = true;
            updateCrosshairs();
        } else {
            advancedViewerState.crosshairsEnabled = false;
            ['axial', 'sagittal', 'coronal'].forEach(orient => renderViewport(orient));
        }
    });

    $('#tool_reset').off('click').on('click', function() {
        resetViewports();
    });
}

/**
 * Reset all viewports to default view
 */
function resetViewports() {
    ['axial', 'sagittal', 'coronal'].forEach(orientation => {
        advancedViewerState.zoom[orientation] = 1;
        advancedViewerState.pan[orientation] = { x: 0, y: 0 };
    });

    const volumeInfo = advancedViewerState.volumeInfo;
    advancedViewerState.sliceIndices.axial = Math.floor(volumeInfo.dimensions.slices / 2);
    advancedViewerState.sliceIndices.sagittal = Math.floor(volumeInfo.dimensions.columns / 2);
    advancedViewerState.sliceIndices.coronal = Math.floor(volumeInfo.dimensions.rows / 2);

    advancedViewerState.windowWidth = volumeInfo.windowing.default_width;
    advancedViewerState.windowLevel = volumeInfo.windowing.default_center;
    updateWindowingDisplay();

    advancedViewerState.imageCache = {};
    loadAllViewports();
}

/**
 * Setup 4D playback controls
 */
function setup4DControls(numTimepoints) {
    $('#controls_4d').show();
    $('#timepoint_max').text(numTimepoints);
    $('#timepoint_slider').attr('max', numTimepoints).val(1);
    $('#timepoint_value').text(1);

    $('#timepoint_slider').off('input').on('input', function() {
        const timepoint = parseInt($(this).val());
        setTimepoint(timepoint);
    });

    $('#btn_play').off('click').on('click', startPlayback);
    $('#btn_pause').off('click').on('click', pausePlayback);
    $('#btn_stop').off('click').on('click', stopPlayback);
}

function setTimepoint(timepoint) {
    advancedViewerState.currentTimepoint = timepoint;
    $('#timepoint_value').text(timepoint);
    $('#timepoint_slider').val(timepoint);
}

function startPlayback() {
    if (advancedViewerState.isPlaying) return;
    advancedViewerState.isPlaying = true;
    $('#btn_play').hide();
    $('#btn_pause').show();

    const maxTimepoints = advancedViewerState.volumeInfo.dimensions.timepoints;
    advancedViewerState.playInterval = setInterval(() => {
        let next = advancedViewerState.currentTimepoint + 1;
        if (next > maxTimepoints) next = 1;
        setTimepoint(next);
    }, 100);
}

function pausePlayback() {
    advancedViewerState.isPlaying = false;
    $('#btn_play').show();
    $('#btn_pause').hide();
    if (advancedViewerState.playInterval) {
        clearInterval(advancedViewerState.playInterval);
        advancedViewerState.playInterval = null;
    }
}

function stopPlayback() {
    pausePlayback();
    setTimepoint(1);
}

/**
 * Close and cleanup the advanced viewer
 */
function closeAdvancedDicomViewer() {
    pausePlayback();

    // Clear all timers
    if (advancedViewerState.windowingDebounceTimer) {
        clearTimeout(advancedViewerState.windowingDebounceTimer);
        advancedViewerState.windowingDebounceTimer = null;
    }
    if (advancedViewerState.scrollEndTimer) {
        clearTimeout(advancedViewerState.scrollEndTimer);
        advancedViewerState.scrollEndTimer = null;
    }
    if (advancedViewerState.pendingScrollRender) {
        cancelAnimationFrame(advancedViewerState.pendingScrollRender);
        advancedViewerState.pendingScrollRender = null;
    }

    // Remove keyboard handler
    $(document).off('keydown.advancedViewer');

    // Reset all state
    advancedViewerState.taskId = null;
    advancedViewerState.selectedSeriesUid = null;
    advancedViewerState.volumeInfo = null;
    advancedViewerState.is4D = false;
    advancedViewerState.currentTimepoint = 1;
    advancedViewerState.imageCache = {};
    advancedViewerState.loadingImages = {};
    advancedViewerState.pendingRequests = {};
    advancedViewerState.activePreloads = 0;
    advancedViewerState.isScrolling = false;
    advancedViewerState.crosshairsEnabled = true;
    advancedViewerState.activeTool = 'crosshairs';
    advancedViewerState.colormap = 'gray';
    advancedViewerState.showColorbar = false;

    ['axial', 'sagittal', 'coronal'].forEach(orientation => {
        advancedViewerState.zoom[orientation] = 1;
        advancedViewerState.pan[orientation] = { x: 0, y: 0 };
        advancedViewerState.sliceIndices[orientation] = 0;
    });

    $('#controls_4d').hide();
    $('#colormap_select').val('gray');
    $('#colorbar_toggle').prop('checked', false);
    $('#interpolation_toggle').prop('checked', true);

    // Reset view layout
    advancedViewerState.viewLayout = 'all';
    setViewLayout('all');

    ['axial', 'sagittal', 'coronal'].forEach(orientation => {
        const canvas = document.getElementById('canvas_' + orientation);
        if (canvas) {
            const ctx = canvas.getContext('2d');
            ctx.clearRect(0, 0, canvas.width, canvas.height);
        }
    });

    $('#advanced_dicom_viewer_modal').removeClass('is-active');
}

// Handle window resize
window.addEventListener('resize', () => {
    if (advancedViewerState.taskId) {
        ['axial', 'sagittal', 'coronal'].forEach(orientation => {
            renderViewport(orientation);
        });
    }
});

/**
 * Show series selector modal for a task with multiple series
 * @param {string} taskId - The task ID
 * @param {Array} seriesList - List of series objects from the API
 * @param {Array} filesList - List of files from the API (to find PDF filename)
 */
function showSeriesSelector(taskId, seriesList, filesList = []) {
    // Store for later use
    window._seriesSelectorTaskId = taskId;
    window._seriesSelectorFiles = filesList;

    // Build the series list HTML
    let html = '';
    seriesList.forEach((series, index) => {
        const desc = series.series_description || 'Series ' + (series.series_number || index + 1);
        const isPdf = series.is_pdf === true;
        const thumbnailUrl = isPdf
            ? ''
            : `/api/task-dicom-thumbnail/${taskId}/${encodeURIComponent(series.thumbnail_file)}`;
        const tagClass = isPdf ? 'is-danger is-light' : 'is-info is-light';
        const countText = isPdf ? 'PDF Document' : `${series.instance_count} images`;
        const iconHtml = isPdf
            ? '<i class="fas fa-file-pdf fa-3x" style="color: #e74c3c;"></i>'
            : `<img src="${thumbnailUrl}" alt="${desc}" onerror="this.parentElement.innerHTML='<i class=\\'fas fa-images fa-2x\\' style=\\'color: #888;\\'></i>'">`;

        html += `
            <div class="series-item ${isPdf ? 'is-pdf' : ''}" data-series-uid="${series.series_uid}" data-is-pdf="${isPdf}" data-thumbnail-file="${series.thumbnail_file || ''}">
                <div class="series-thumbnail">
                    ${iconHtml}
                </div>
                <div class="series-info">
                    <div class="series-title">${desc}</div>
                    <div class="series-meta">
                        <span class="tag ${tagClass}">${series.modality}</span>
                        <span class="series-count">${countText}</span>
                    </div>
                </div>
            </div>
        `;
    });

    $('#series_selector_list').html(html);
    $('#series_selector_task_id').data('taskId', taskId);
    $('#series_selector_modal').addClass('is-active');

    // Setup click handlers for series items
    $('.series-item').off('click').on('click', function() {
        const seriesUid = $(this).data('series-uid');
        const isPdf = $(this).data('is-pdf') === true || $(this).data('is-pdf') === 'true';
        const thumbnailFile = $(this).data('thumbnail-file');
        closeSeriesSelector();

        if (isPdf) {
            // Find PDF file and open PDF viewer
            const pdfFile = window._seriesSelectorFiles.find(f => f.is_pdf);
            if (pdfFile) {
                openPdfViewer(taskId, pdfFile.filename);
            }
        } else {
            openAdvancedDicomViewer(taskId, seriesUid);
        }
    });
}

/**
 * Close the series selector modal
 */
function closeSeriesSelector() {
    $('#series_selector_modal').removeClass('is-active');
}

// Export functions
window.openAdvancedDicomViewer = openAdvancedDicomViewer;
window.closeAdvancedDicomViewer = closeAdvancedDicomViewer;
window.showSeriesSelector = showSeriesSelector;
window.closeSeriesSelector = closeSeriesSelector;
