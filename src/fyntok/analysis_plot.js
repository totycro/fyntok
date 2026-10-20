import NativePlotly from "fyntok-plotly-base/plotly.js";

export default {
  extends: NativePlotly,
  methods: {
    update() {
      // NiceGUI 3.18 observes hidden/deactivated tab panels as well as visible ones.
      // Plotly rejects resizing those elements (and plots still being initialized).
      // Wrap this component's reference, preserving the shared Plotly library.
      if (this.Plotly && !this.resizeGuardInstalled) {
        const native = this.Plotly;
        this.Plotly = {
          ...native,
          Plots: {
            ...native.Plots,
            resize: (element) => {
              if (!element?.isConnected || !element._fullLayout || !element.getClientRects().length) {
                return Promise.resolve();
              }
              return native.Plots.resize(element);
            },
          },
        };
        this.resizeGuardInstalled = true;
      }
      NativePlotly.methods.update.call(this);
    },
  },
};
