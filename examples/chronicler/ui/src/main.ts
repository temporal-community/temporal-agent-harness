import { mount } from "svelte";
import ChroniclerApp from "./ChroniclerApp.svelte";
import "$ui/app.css";

const target = document.getElementById("app");
if (!target) throw new Error("Missing #app mount target");

mount(ChroniclerApp, { target });
