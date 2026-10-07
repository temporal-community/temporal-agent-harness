import { mount } from "svelte";
import App from "./App.svelte";
import { quietProgrammaticTips } from "./lib/components/primitives/tipFocus";
import "./app.css";

quietProgrammaticTips(document);

const target = document.getElementById("app");
if (!target) throw new Error("Missing #app mount target");

mount(App, { target });
