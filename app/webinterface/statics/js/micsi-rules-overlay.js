/*
 * micsi-rules-overlay.js — patient-level rule triggers for the rules editor.
 * =========================================================================
 *
 * Upstream's rules_edit.html offers "Completed Series" and "Completed Study"
 * as action triggers. MICSI also routes on "Completed Patient", which needs a
 * third option plus its own set of trigger fields.
 *
 * The "Completed Patient" option and the {% include %} for its fields are
 * rendered server-side in rules_edit.html, so the saved value round-trips
 * through Jinja like upstream's own options. The behaviour lives here, bound
 * as ADDITIONAL jQuery
 * handlers rather than by editing upstream's. jQuery runs every handler bound
 * to an event, so upstream keeps managing the study fields and this manages
 * the patient ones. Nothing here has to be re-merged when upstream changes
 * its own logic.
 *
 * Backend support is already in place: webinterface/rules.py reads all six
 * patient_trigger_* form fields, and common/types.py defines them on Rule.
 */

(function () {
  "use strict";

  function applyPatientVisibility() {
    var trigger = $("#action_trigger").children("option:selected").val();
    if (trigger === "patient") {
      $("#patient_trigger_field").show();
      $("#force_patient_completion_field").show();
      // upstream's handler has no 'patient' branch, so clear its fields here
      $("#study_trigger_field").hide();
      $("#force_study_completion_field").hide();
      $("#study_trigger_series").prop("required", false);
    } else {
      $("#patient_trigger_field").hide();
      $("#force_patient_completion_field").hide();
      $("#patient_trigger_modalities, #patient_trigger_studies, #patient_trigger_series")
        .prop("required", false);
    }
  }

  $('#patient_trigger_condition').change(function () {
              var patient_trigger = $(this).children("option:selected").val();
  
              if (patient_trigger == 'timeout') {
                  $('#patient_trigger_modalities_section').hide();
                  $('#patient_trigger_studies_section').hide();
                  $('#patient_trigger_series_section').hide();
                  $('#patient_trigger_modalities').prop('required', false);
                  $('#patient_trigger_studies').prop('required', false);
                  $('#patient_trigger_series').prop('required', false);
              } else if (patient_trigger == 'received_modalities') {
                  $('#patient_trigger_modalities_section').show();
                  $('#patient_trigger_studies_section').hide();
                  $('#patient_trigger_series_section').hide();
                  $('#patient_trigger_modalities').prop('required', true);
                  $('#patient_trigger_studies').prop('required', false);
                  $('#patient_trigger_series').prop('required', false);
              } else if (patient_trigger == 'received_studies') {
                  $('#patient_trigger_modalities_section').hide();
                  $('#patient_trigger_studies_section').show();
                  $('#patient_trigger_series_section').hide();
                  $('#patient_trigger_modalities').prop('required', false);
                  $('#patient_trigger_studies').prop('required', true);
                  $('#patient_trigger_series').prop('required', false);
              } else if (patient_trigger == 'received_series') {
                  $('#patient_trigger_modalities_section').hide();
                  $('#patient_trigger_studies_section').hide();
                  $('#patient_trigger_series_section').show();
                  $('#patient_trigger_modalities').prop('required', false);
                  $('#patient_trigger_studies').prop('required', false);
                  $('#patient_trigger_series').prop('required', true);
              }
          });

  // Strip characters that would break the JSON round-trip, matching the
  // sanitisation upstream applies to its own trigger inputs.
  function bindSanitisers() {
    $("#patient_trigger_modalities, #patient_trigger_studies, #patient_trigger_series")
      .on("keyup", function (evt) {
        evt.target.value = evt.target.value.replace(/[{}"\n]/g, "");
      });
  }

  $(function () {
    try {
      bindSanitisers();
      $("#action_trigger").on("change", applyPatientVisibility);
      applyPatientVisibility();
      $("#patient_trigger_condition").trigger("change");
    } catch (e) {
      if (window.console) console.error("[micsi-overlay] rules overlay failed:", e);
    }
  });
})();
